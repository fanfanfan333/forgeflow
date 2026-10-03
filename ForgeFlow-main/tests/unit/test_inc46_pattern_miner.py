"""INC46 T02 — Pattern Miner: every metric is recomputed by hand in the test.

The point of this suite is that **no** number is taken on faith: for a
hand-built trace the expected ``frequency`` / ``success_rate`` /
``tool_consistency`` / ``output_consistency`` / ``pattern_score`` are written
out literally, and the miner must reproduce them. It also pins the honesty red
line (zero denominator ⇒ ``None``, never ``0``) and the qualification rule
(score ≥ threshold **and** support ≥ min).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from forgeflow.experience.embedding import deterministic_embedding
from forgeflow.experience.models import ExperienceRecord
from forgeflow.repositories.memory.experience_repo import MemoryExperienceRepository
from forgeflow.repositories.memory.skill_repo import MemorySkillCandidateRepository
from forgeflow.skills.candidate_compiler import compile_candidate, pattern_metrics
from forgeflow.skills.pattern_miner import (
    PATTERN_SCORE_THRESHOLD,
    PATTERN_WEIGHTS,
    derive_run_outcome,
    mine_patterns,
    mine_tenant_patterns,
    pattern_key,
    qualifies,
    run_output_keyset,
    safe_ratio,
    score_components,
)

# ``asyncio_mode = "auto"`` (pyproject) collects the async cases; the pure
# (synchronous) cases must NOT carry an asyncio mark, so none is applied here.


# --------------------------------------------------------------------------- #
# trace-like fixtures                                                          #
# --------------------------------------------------------------------------- #
def _step(
    tool: str,
    *,
    status: str = "ok",
    run_id: str = "r1",
    index: int = 0,
    payload: dict | None = None,
    created_at: str | None = None,
) -> dict:
    s: dict = {"tool": tool, "status": status, "run_id": run_id, "step_index": index}
    if payload is not None:
        s["output"] = {"payload": payload}
    if created_at is not None:
        s["created_at"] = created_at
    return s


def _run(
    run_id: str,
    tools: list[str],
    *,
    statuses: list[str] | None = None,
    payloads: list[dict | None] | None = None,
    created_at: str | None = None,
) -> list[dict]:
    statuses = statuses or ["ok"] * len(tools)
    payloads = payloads or [None] * len(tools)
    return [
        _step(t, status=st, run_id=run_id, index=i, payload=pl, created_at=created_at)
        for i, (t, st, pl) in enumerate(zip(tools, statuses, payloads))
    ]


#: R=4 runs: raw sequences (a,a,b)/(a,b,b)/(a,b)/(a,b) all collapse to "a→b".
_FIXTURE_RUNS = [
    _run("r1", ["a", "a", "b"], payloads=[{"k1": 1}, None, None]),
    _run("r2", ["a", "b", "b"], payloads=[{"k1": 1}, None, None]),
    _run("r3", ["a", "b"], payloads=[{"k1": 1}, None]),
    _run("r4", ["a", "b"], statuses=["ok", "error"], payloads=[{"k2": 2}, None]),
]


# --------------------------------------------------------------------------- #
# 1. pattern_key normalisation                                                 #
# --------------------------------------------------------------------------- #
def test_pattern_key_collapses_only_adjacent_duplicates():
    assert pattern_key(["a", "a", "b", "a"]) == "a→b→a"
    assert pattern_key(["a", "b", "b"]) == "a→b"
    assert pattern_key([]) == ""
    assert pattern_key(["", "a", None, "a"]) == "a"  # empty tools dropped
    # distinct-order preserved (non-adjacent repeat kept)
    assert pattern_key(["x", "y", "x"]) == "x→y→x"


def test_derivations_are_recomputable():
    assert derive_run_outcome(_run("r", ["a", "b"])) == "success"
    assert derive_run_outcome(_run("r", ["a"], statuses=["error"])) == "failure"
    assert derive_run_outcome(_run("r", ["a"], statuses=["unavailable"])) == "failure"
    assert derive_run_outcome(_run("r", ["a"], statuses=["blocked"])) == "success"
    assert run_output_keyset(_run("r", ["a"], payloads=[{"z": 1, "a": 2}])) == ("a", "z")


# --------------------------------------------------------------------------- #
# 2. the four metrics, recomputed literally                                    #
# --------------------------------------------------------------------------- #
def test_mine_patterns_recomputes_every_metric():
    patterns = mine_patterns(_FIXTURE_RUNS, min_support=1)
    assert len(patterns) == 1
    p = patterns[0]
    assert p.key == "a→b"
    assert p.tools == ["a", "b"]
    m = p.metrics
    # frequency = N/R = 4/4
    assert m.frequency == 1.0
    # success_rate = 3/4 (r4 carries an error step)
    assert m.success_rate == 0.75
    # tool_consistency = modal raw seq (a,b)x2 / 4
    assert m.tool_consistency == 0.5
    # output_consistency = modal keyset ("k1",)x3 / 4
    assert m.output_consistency == 0.75
    # pattern_score = .25*1.0 + .35*0.75 + .20*0.5 + .20*0.75
    assert m.pattern_score == 0.7625
    assert m.support == 4 and m.sample_size == 4
    assert set(p.source_run_ids) == {"r1", "r2", "r3", "r4"}


def test_to_dict_emits_full_intermediate_quantities():
    p = mine_patterns(_FIXTURE_RUNS, min_support=1)[0]
    d = p.to_dict()["metrics"]
    assert d["frequency_numerator"] == 4 and d["frequency_denominator"] == 4
    assert d["success_numerator"] == 3 and d["success_denominator"] == 4
    assert d["tool_consistency_numerator"] == 2 and d["tool_consistency_denominator"] == 4
    assert d["output_consistency_numerator"] == 3 and d["output_consistency_denominator"] == 4
    assert d["support"] == 4 and d["sample_size"] == 4


def test_example_step_ids_are_runtime_shaped():
    p = mine_patterns(_FIXTURE_RUNS[:1], min_support=1)[0]
    # r1 has 3 steps (indices 0,1,2) and no attempt ⇒ "{run_id}:{index}".
    assert p.example_step_ids == ["r1:0", "r1:1", "r1:2"]


# --------------------------------------------------------------------------- #
# 3. weights + threshold + zero denominator                                    #
# --------------------------------------------------------------------------- #
def test_weights_sum_to_one_and_cover_four_metrics():
    assert set(PATTERN_WEIGHTS) == {
        "frequency",
        "success_rate",
        "tool_consistency",
        "output_consistency",
    }
    assert sum(PATTERN_WEIGHTS.values()) == 1.0
    assert PATTERN_SCORE_THRESHOLD == 0.60


def test_zero_denominator_is_none_never_zero():
    assert safe_ratio(0, 0) is None
    assert safe_ratio(7, 0) is None
    assert safe_ratio(0, 0) != 0
    assert safe_ratio(1, 4) == 0.25


def test_score_is_none_when_any_component_is_none():
    good = {"frequency": 1.0, "success_rate": 0.5, "tool_consistency": 1.0, "output_consistency": 1.0}
    assert score_components(good) is not None
    for missing in PATTERN_WEIGHTS:
        broken = dict(good, **{missing: None})
        assert score_components(broken) is None


def test_empty_runs_yield_no_pattern():
    assert mine_patterns([], min_support=1) == []


def test_min_support_filters_groups():
    assert mine_patterns(_FIXTURE_RUNS, min_support=5) == []
    assert len(mine_patterns(_FIXTURE_RUNS, min_support=4)) == 1


# --------------------------------------------------------------------------- #
# 4. qualification = score ≥ threshold AND support ≥ min                        #
# --------------------------------------------------------------------------- #
def test_qualification_threshold_and_support():
    runs = [
        _run("s1", ["a", "b"]),
        _run("s2", ["a", "b"]),
        _run("f1", ["a", "c"], statuses=["ok", "error"]),
        _run("f2", ["a", "c"], statuses=["ok", "error"]),
    ]
    patterns = mine_patterns(runs, min_support=2)
    by_key = {p.key: p for p in patterns}
    assert set(by_key) == {"a→b", "a→c"}
    # "a→b": score .25*.5 + .35*1 + .2*1 + .2*1 = .875 ⇒ qualified at support 2.
    assert by_key["a→b"].metrics.pattern_score == 0.875
    assert qualifies(by_key["a→b"].metrics, min_support=2) is True
    # "a→c": success_rate 0 ⇒ score .125 + 0 + .2 + .2 = .525 ⇒ below threshold.
    assert by_key["a→c"].metrics.pattern_score == 0.525
    assert qualifies(by_key["a→c"].metrics, min_support=2) is False
    # Raising the support bar disqualifies by support even when the score clears.
    assert qualifies(by_key["a→b"].metrics, min_support=3) is False
    # Sorted by score desc.
    assert patterns[0].key == "a→b"


def test_undefined_score_never_qualifies():
    p = mine_patterns(_FIXTURE_RUNS, min_support=1)[0]
    p.metrics.pattern_score = None
    assert qualifies(p.metrics, min_support=1) is False


# --------------------------------------------------------------------------- #
# 5. tenant read path (fail-closed + window)                                   #
# --------------------------------------------------------------------------- #
async def test_mine_tenant_patterns_is_fail_closed_without_tenant():
    assert await mine_tenant_patterns(None) == []
    assert await mine_tenant_patterns("") == []


async def test_mine_tenant_patterns_reads_trace_and_windows(monkeypatch):
    from forgeflow.runtime import trace_store

    fresh = _run("run-a", ["a", "b"], created_at="2099-01-01T00:00:00+00:00")
    fresh2 = _run("run-b", ["a", "b"], created_at="2099-01-01T00:00:00+00:00")
    stale = _run("run-old", ["z"], created_at="2000-01-01T00:00:00+00:00")

    async def _fake_list(tenant_id, *, limit=1000):  # noqa: ANN001
        return fresh + fresh2 + stale

    monkeypatch.setattr(trace_store, "list_for_tenant", _fake_list)
    patterns = await mine_tenant_patterns("t-inc46", window_days=30, min_support=2)
    keys = {p.key for p in patterns}
    assert keys == {"a→b"}  # stale run excluded by the window
    assert patterns[0].metrics.sample_size == 2


# --------------------------------------------------------------------------- #
# 6. candidate_compiler integration (additive)                                 #
# --------------------------------------------------------------------------- #
def _exp(outcome: str, tools: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        outcome=outcome,
        run_id=f"run-{uuid.uuid4().hex[:6]}",
        reusable_steps=[{"tool": t} for t in tools],
    )


def test_pattern_metrics_shape_is_recomputable():
    exps = [_exp("success", ["a", "b"]) for _ in range(3)]
    m = pattern_metrics(exps)
    assert m["pattern_key"] == "a→b"
    assert m["weights"] == PATTERN_WEIGHTS
    assert m["score_threshold"] == PATTERN_SCORE_THRESHOLD
    assert m["run_count"] == 3 and m["pattern_count"] == 1
    assert m["metrics"]["support"] == 3 and m["metrics"]["sample_size"] == 3
    assert m["metrics"]["pattern_score"] == 1.0
    assert m["qualified"] is True


def test_pattern_metrics_projects_run_outcome_into_success_rate():
    exps = [_exp("success", ["a", "b"]), _exp("failure", ["a", "b"])]
    m = pattern_metrics(exps)
    assert m["metrics"]["success_rate"] == 0.5


def test_pattern_metrics_empty_is_honest():
    m = pattern_metrics([])
    assert m["pattern_key"] == ""
    assert m["metrics"] == {}
    assert m["qualified"] is False
    assert m["run_count"] == 0 and m["pattern_count"] == 0


async def test_compile_candidate_fills_pattern_metrics_additively():
    tenant = f"t-inc46pat-{uuid.uuid4().hex[:8]}"
    exp_repo = MemoryExperienceRepository()
    cand_repo = MemorySkillCandidateRepository()
    embedding = deterministic_embedding("分析销售数据 数据分析")
    for i in range(3):
        await exp_repo.save(
            ExperienceRecord(
                tenant_id=tenant,
                run_id=f"r-{i}",
                summary="分析销售数据 — 2 步，结果：success",
                outcome="success",
                reusable_steps=[{"tool": "data.query"}, {"tool": "report.render"}],
                tags=["数据分析"],
                embedding=embedding,
            )
        )

    candidate = await compile_candidate(
        tenant, mode="auto", experience_repo=exp_repo, candidate_repo=cand_repo
    )
    assert candidate.status == "draft"
    spec = candidate.draft_spec
    # Existing keys untouched …
    for key in ("prompt", "steps", "tools", "io_schema", "applicable_when"):
        assert key in spec, key
    # … plus the new additive key.
    assert "pattern_metrics" in spec
    assert spec["pattern_metrics"]["weights"] == PATTERN_WEIGHTS
    assert spec["pattern_metrics"]["pattern_key"] == "data.query→report.render"
