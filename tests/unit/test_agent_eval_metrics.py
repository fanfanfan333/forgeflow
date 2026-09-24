"""INC8 Phase-B / T01 — the pure 12-metric layer (``agent_metrics``).

Covers the design's test plan (docs/sop/11-INC8-AGENT-EVALUATION-DESIGN.md §8):

  * given a constructed ``RunRecord`` cohort → #1/#10/#11/#12 have **exact**
    values (success rate, avg cost, p50/p95 latency, recovery rate);
  * an empty store → those four are ``value=None`` + ``has_data=False`` — **not**
    a fabricated ``0.0``;
  * the five ⛔ metrics are ``computability="not_available"`` with ``value=None``;
  * the three 🟡 metrics are ``computability="needs_instrumentation"``.

The non-vacuity (inject → must go red) proof lives in a separate pytest plugin
(see the T05 evidence products), not here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from forgeflow.evaluation.agent_metrics import (
    COMPUTABILITY_NEEDS_INSTRUMENTATION,
    COMPUTABILITY_NOT_AVAILABLE,
    COMPUTABILITY_OK,
    SPECS,
    AgentEvalSnapshot,
    aggregate,
    evaluate_run,
    spec_for,
)
from forgeflow.repositories.eval_sample_repo import EvalSample
from forgeflow.runtime.orchestrator import RunRecord

_BASE = datetime(2026, 9, 24, 10, 0, 0, tzinfo=timezone.utc)


def _record(
    run_id: str,
    *,
    status: str,
    offset_ms: int,
    cost: float,
    tokens: int = 0,
    observations: int = 0,
    skills: list[str] | None = None,
    plan_source: str | None = None,
    dropped_tools: list[str] | None = None,
) -> RunRecord:
    created = _BASE
    completed = _BASE + timedelta(milliseconds=offset_ms)
    llm: dict = {}
    if plan_source is not None or dropped_tools is not None:
        llm = {
            "plan": {
                "source": plan_source or "fallback",
                "steps": ["research.search", "data.query"],
                "dropped_tools": list(dropped_tools or []),
            }
        }
    record = RunRecord(
        run_id=run_id,
        thread_id=f"thread-{run_id}",
        tenant_id="tenant-a",
        agent_id=None,
        intent="do a thing",
        status=status,
        outcome="task_completed" if status == "completed" else "task_failed",
        steps=[],
        errors=[] if status == "completed" else ["boom"],
        created_at=created.isoformat(),
        completed_at=completed.isoformat(),
        total_tokens=tokens,
        total_cost_usd=cost,
        runtime_mode="deterministic",
        llm=llm,
        loop={"observations": observations, "tripped": False, "breaches": []},
    )
    if skills is not None:
        record.skills_used = skills
    if plan_source is not None:
        record.plan_source = plan_source
    return record


def _cohort() -> list[RunRecord]:
    """4 runs: 3 completed / 1 failed; two needed a replan; one recovered."""
    return [
        _record("r1", status="completed", offset_ms=100, cost=0.02, skills=["skill-a"]),
        _record("r2", status="completed", offset_ms=200, cost=0.04, skills=["skill-a"]),
        _record("r3", status="failed", offset_ms=300, cost=0.06, observations=2),
        _record(
            "r4",
            status="completed",
            offset_ms=400,
            cost=0.08,
            observations=2,
            skills=["skill-b"],
        ),
    ]


# --------------------------------------------------------------------------- #
# evaluate_run — pure per-run reduction                                        #
# --------------------------------------------------------------------------- #
def test_evaluate_run_reads_the_real_signals():
    rec = _record(
        "r1",
        status="completed",
        offset_ms=250,
        cost=0.05,
        tokens=42,
        observations=1,
        skills=["s1", "s2"],
        plan_source="llm",
        dropped_tools=["payment.transfer"],
    )
    sig = evaluate_run(rec)
    assert sig["duration_ms"] == 250.0
    assert sig["cost_usd"] == 0.05
    assert sig["tokens"] == 42
    assert sig["terminal"] is True
    assert sig["completed"] is True
    assert sig["needs_replan"] is True
    assert sig["recovered"] is True
    assert sig["plan_source"] == "llm"
    assert sig["dropped_tools"] == 1
    assert sig["planned_steps"] == 2
    assert sig["skills_count"] == 2
    assert sig["has_skill_signal"] is True


def test_evaluate_run_works_on_a_plain_mapping():
    sig = evaluate_run(
        {
            "status": "failed",
            "created_at": _BASE.isoformat(),
            "completed_at": (_BASE + timedelta(milliseconds=500)).isoformat(),
            "total_cost_usd": 0.1,
        }
    )
    assert sig["duration_ms"] == 500.0
    assert sig["terminal"] is True
    assert sig["completed"] is False
    assert sig["needs_replan"] is False


# --------------------------------------------------------------------------- #
# aggregate — exact values for the 4 computable metrics                        #
# --------------------------------------------------------------------------- #
def test_computable_metrics_have_exact_values():
    snap = aggregate(_cohort())
    assert isinstance(snap, AgentEvalSnapshot)
    assert snap.sample_runs == 4

    success = snap.quality["task_success_rate"]
    assert success.value == 0.75  # 3 completed / 4 terminal
    assert success.has_data is True
    assert success.computability == COMPUTABILITY_OK

    cost = snap.cost["cost_per_task"]
    assert cost.value == 0.05  # (0.02+0.04+0.06+0.08)/4
    assert cost.computability == COMPUTABILITY_OK

    p50 = snap.reliability["latency_p50_ms"]
    p95 = snap.reliability["latency_p95_ms"]
    assert p50.value == 300.0  # nearest-rank on [100,200,300,400]
    assert p95.value == 400.0

    recovery = snap.reliability["failure_recovery_rate"]
    assert recovery.value == 0.5  # 1 recovered / 2 that needed replanning
    assert recovery.has_data is True


def test_skill_reuse_rate_counts_runs_with_a_reused_skill():
    snap = aggregate(_cohort())
    skill = snap.quality["skill_reuse_rate"]
    assert skill.computability == COMPUTABILITY_NEEDS_INSTRUMENTATION
    assert skill.value == 0.75  # 3 of 4 runs carried a skills_used signal
    assert skill.has_data is True


# --------------------------------------------------------------------------- #
# empty cohort — honest "no data" (never a fabricated 0)                       #
# --------------------------------------------------------------------------- #
def test_empty_store_is_null_not_zero_for_computable_metrics():
    snap = aggregate([])
    assert snap.sample_runs == 0
    for name in ("task_success_rate",):
        metric = snap.quality[name]
        assert metric.value is None
        assert metric.has_data is False
    assert snap.cost["cost_per_task"].value is None
    assert snap.cost["cost_per_task"].has_data is False
    for name in ("latency_p50_ms", "latency_p95_ms", "failure_recovery_rate"):
        metric = snap.reliability[name]
        assert metric.value is None, name
        assert metric.has_data is False, name


def test_metrics_with_no_ground_truth_are_not_available():
    snap = aggregate(_cohort())
    for name in (
        "tool_selection_accuracy",
        "planning_accuracy",
        "retrieval_recall",
        "citation_accuracy",
        "memory_recall",
    ):
        metric = snap.quality[name]
        assert metric.computability == COMPUTABILITY_NOT_AVAILABLE, name
        assert metric.value is None, name
        assert metric.has_data is False, name


def test_judge_metrics_need_instrumentation_without_samples():
    snap = aggregate(_cohort())  # no judge samples
    for name in ("groundedness", "hallucination_rate"):
        metric = snap.quality[name]
        assert metric.computability == COMPUTABILITY_NEEDS_INSTRUMENTATION, name
        assert metric.value is None, name
        assert metric.has_data is False, name
    # skill_reuse is 🟡 even when it can be computed from run data.
    assert (
        snap.quality["skill_reuse_rate"].computability
        == COMPUTABILITY_NEEDS_INSTRUMENTATION
    )


def test_groundedness_and_hallucination_use_persisted_samples():
    samples = [
        EvalSample(metric_name="groundedness", metric_value=0.8, dimension="quality"),
        EvalSample(metric_name="groundedness", metric_value=0.6, dimension="quality"),
        EvalSample(metric_name="hallucination", metric_value=1.0, dimension="quality"),
        EvalSample(metric_name="hallucination", metric_value=0.0, dimension="quality"),
        EvalSample(metric_name="other", metric_value=9.9, dimension="quality"),
    ]
    snap = aggregate(_cohort(), samples)
    grd = snap.quality["groundedness"]
    assert grd.value == 0.7  # (0.8+0.6)/2
    assert grd.has_data is True
    assert grd.computability == COMPUTABILITY_NEEDS_INSTRUMENTATION
    hal = snap.quality["hallucination_rate"]
    assert hal.value == 0.5  # 1 flagged / 2 evaluated
    assert hal.has_data is True
    assert snap.judged_samples == 5


# --------------------------------------------------------------------------- #
# snapshot payload + catalogue invariants                                      #
# --------------------------------------------------------------------------- #
def test_snapshot_to_dict_is_json_shaped_with_nulls():
    payload = aggregate(_cohort(), source="hub_runs", durable=False).to_dict()
    assert payload["source"] == "hub_runs"
    assert payload["durable"] is False
    assert payload["sample_runs"] == 4
    assert set(payload["quality"]["metrics"]) >= {
        "task_success_rate",
        "tool_selection_accuracy",
        "groundedness",
    }
    # A ⛔ metric serialises as a real JSON null, not 0.
    assert payload["quality"]["metrics"]["citation_accuracy"]["value"] is None
    assert payload["quality"]["metrics"]["citation_accuracy"]["computability"] == (
        COMPUTABILITY_NOT_AVAILABLE
    )


def test_specs_cover_exactly_the_12_metric_catalogue():
    names = {spec.name for spec in SPECS}
    assert names == {
        "task_success_rate",
        "tool_selection_accuracy",
        "planning_accuracy",
        "retrieval_recall",
        "citation_accuracy",
        "groundedness",
        "hallucination_rate",
        "memory_recall",
        "skill_reuse_rate",
        "cost_per_task",
        "latency_p50_ms",
        "latency_p95_ms",
        "failure_recovery_rate",
    }
    # computability split: 5 ✅ (incl. the two latency keys), 3 🟡, 5 ⛔.
    ok = [s for s in SPECS if s.computability == COMPUTABILITY_OK]
    needs = [s for s in SPECS if s.computability == COMPUTABILITY_NEEDS_INSTRUMENTATION]
    na = [s for s in SPECS if s.computability == COMPUTABILITY_NOT_AVAILABLE]
    assert len(ok) == 5
    assert len(needs) == 3
    assert len(na) == 5
    assert spec_for("groundedness") is not None
    assert spec_for("nope") is None


def test_aggregate_reports_source_and_durability_flags():
    snap = aggregate(_cohort(), source="postgres", durable=True, window_days=7)
    assert snap.source == "postgres"
    assert snap.durable is True
    assert snap.window_days == 7
    assert snap.generated_at
