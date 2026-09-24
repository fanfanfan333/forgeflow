"""INC8 Phase-B / T02 — the dual-backend ``AgentEvalSource``.

Pins the honesty contract from the design (docs/sop/11-INC8-AGENT-EVALUATION-DESIGN.md
§7):

  * the memory source reads the hub run store and reports ``source="hub_runs"`` +
    ``durable=False`` — a process-lifetime number, exposed not hidden;
  * the PG source reads ``workflow_runs`` + ``agent_eval_samples`` and reports
    ``source="postgres"`` + ``durable=True``; the two sources are never mixed;
  * a metric a source genuinely cannot compute (per-task skill reuse on a source
    with no skill signal) is ``value=None`` / ``has_data=False`` — never ``0.0``.

Also covers the additive ``RunRecord`` fields (M3) the source depends on.
"""

from __future__ import annotations

import datetime as _dt
from unittest.mock import AsyncMock, MagicMock

import pytest

from forgeflow.evaluation.agent_metrics import COMPUTABILITY_NOT_AVAILABLE
from forgeflow.evaluation.eval_source import (
    MemoryAgentEvalSource,
    PostgresAgentEvalSource,
    get_agent_eval_source,
    reset_agent_eval_source,
)
from forgeflow.repositories.eval_sample_repo import EvalSample, clear_eval_sample_store
from forgeflow.runtime.orchestrator import RunRecord, get_run_store, reset_run_store

pytestmark = pytest.mark.asyncio

_UTC = _dt.timezone.utc
_BASE = _dt.datetime(2026, 9, 24, 9, 0, 0, tzinfo=_UTC)


def _record(
    run_id: str,
    *,
    status: str,
    offset_ms: int,
    cost: float,
    skills: list[str] | None = None,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        thread_id=f"t-{run_id}",
        tenant_id="tenant-a",
        agent_id=None,
        intent="x",
        status=status,
        outcome="ok",
        steps=[],
        errors=[],
        created_at=_BASE.isoformat(),
        completed_at=(_BASE + _dt.timedelta(milliseconds=offset_ms)).isoformat(),
        total_cost_usd=cost,
        plan_source="llm",
        skills_used=list(skills or []),
    )


@pytest.fixture(autouse=True)
def _clean(force_memory_backend):
    reset_run_store()
    clear_eval_sample_store()
    reset_agent_eval_source()
    yield
    reset_run_store()
    clear_eval_sample_store()
    reset_agent_eval_source()


# --------------------------------------------------------------------------- #
# memory source                                                                #
# --------------------------------------------------------------------------- #
async def test_memory_source_empty_store_is_honest_no_data():
    payload = await MemoryAgentEvalSource().summary("tenant-a")
    assert payload["source"] == "hub_runs"
    assert payload["durable"] is False
    assert payload["sample_runs"] == 0
    assert payload["quality"]["metrics"]["task_success_rate"]["value"] is None
    assert payload["reliability"]["metrics"]["latency_p50_ms"]["value"] is None


async def test_memory_source_aggregates_the_hub_run_store():
    store = get_run_store()
    store.save(_record("r1", status="completed", offset_ms=100, cost=0.02, skills=["s-a"]))
    store.save(_record("r2", status="completed", offset_ms=200, cost=0.04, skills=["s-a"]))
    store.save(_record("r3", status="failed", offset_ms=300, cost=0.06))

    payload = await MemoryAgentEvalSource().summary("tenant-a")
    metrics = payload["quality"]["metrics"]
    assert payload["sample_runs"] == 3
    assert metrics["task_success_rate"]["value"] == pytest.approx(2 / 3)
    assert payload["cost"]["metrics"]["cost_per_task"]["value"] == pytest.approx(0.04)
    assert payload["reliability"]["metrics"]["latency_p50_ms"]["value"] == 200.0
    # skill signal is present (skills_used wired) → a real per-task ratio.
    assert metrics["skill_reuse_rate"]["value"] == pytest.approx(2 / 3)


async def test_memory_source_reports_judge_samples():
    from forgeflow.repositories.factory import get_eval_sample_repository

    repo = get_eval_sample_repository()
    await repo.save_sample(
        "tenant-a",
        EvalSample(metric_name="groundedness", metric_value=0.8, dimension="quality"),
    )
    await repo.save_sample(
        "tenant-a",
        EvalSample(metric_name="groundedness", metric_value=0.6, dimension="quality"),
    )
    payload = await MemoryAgentEvalSource().summary("tenant-a")
    assert payload["judged_samples"] == 2
    assert payload["quality"]["metrics"]["groundedness"]["value"] == pytest.approx(0.7)


async def test_memory_source_samples_are_listed_and_filtered():
    from forgeflow.repositories.factory import get_eval_sample_repository

    repo = get_eval_sample_repository()
    await repo.save_sample(
        "tenant-a", EvalSample(metric_name="groundedness", metric_value=0.5)
    )
    await repo.save_sample(
        "tenant-a", EvalSample(metric_name="hallucination", metric_value=1.0)
    )
    src = MemoryAgentEvalSource()
    everything = await src.samples("tenant-a")
    assert len(everything) == 2
    only_hallucination = await src.samples("tenant-a", metric="hallucination")
    assert [s["metric_name"] for s in only_hallucination] == ["hallucination"]


# --------------------------------------------------------------------------- #
# PG source                                                                    #
# --------------------------------------------------------------------------- #
def _pg_pool(run_rows, sample_rows):
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=[run_rows, sample_rows])
    pool = MagicMock()
    pool.acquire = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=conn),
            __aexit__=AsyncMock(return_value=None),
        )
    )
    return pool, conn


async def test_pg_source_reads_workflow_runs_and_reports_durable():
    runs = [
        {
            "status": "completed",
            "created_at": _BASE,
            "completed_at": _BASE + _dt.timedelta(milliseconds=150),
            "total_tokens": 10,
            "total_cost_usd": 0.03,
        },
        {
            "status": "completed",
            "created_at": _BASE,
            "completed_at": _BASE + _dt.timedelta(milliseconds=250),
            "total_tokens": 20,
            "total_cost_usd": 0.05,
        },
    ]
    samples = [{"metric_name": "groundedness", "metric_value": 0.9}]
    pool, _ = _pg_pool(runs, samples)

    payload = await PostgresAgentEvalSource(pool=pool).summary("default")
    assert payload["source"] == "postgres"
    assert payload["durable"] is True
    assert payload["sample_runs"] == 2
    assert payload["quality"]["metrics"]["task_success_rate"]["value"] == 1.0
    assert payload["cost"]["metrics"]["cost_per_task"]["value"] == pytest.approx(0.04)
    assert payload["quality"]["metrics"]["groundedness"]["value"] == pytest.approx(0.9)


async def test_pg_source_skill_reuse_is_unavailable_not_zero():
    """workflow_runs carries no per-run skill signal → honest "no data", not 0.0."""
    runs = [
        {
            "status": "completed",
            "created_at": _BASE,
            "completed_at": _BASE + _dt.timedelta(milliseconds=100),
            "total_tokens": 1,
            "total_cost_usd": 0.0,
        }
    ]
    pool, _ = _pg_pool(runs, [])
    payload = await PostgresAgentEvalSource(pool=pool).summary("default")
    skill = payload["quality"]["metrics"]["skill_reuse_rate"]
    assert skill["value"] is None
    assert skill["has_data"] is False
    # The statically-⛔ metrics stay not_available regardless of source.
    assert (
        payload["quality"]["metrics"]["citation_accuracy"]["computability"]
        == COMPUTABILITY_NOT_AVAILABLE
    )


# --------------------------------------------------------------------------- #
# factory                                                                      #
# --------------------------------------------------------------------------- #
async def test_factory_selects_memory_source_offline():
    src = get_agent_eval_source()
    assert isinstance(src, MemoryAgentEvalSource)
    # Cached per backend — the same instance comes back.
    assert get_agent_eval_source() is src
    reset_agent_eval_source()
    assert get_agent_eval_source() is not src


async def test_factory_selects_postgres_source(monkeypatch):
    from forgeflow.config import get_settings

    monkeypatch.setattr(get_settings(), "storage_backend", "postgres")
    reset_agent_eval_source()
    src = get_agent_eval_source()
    assert isinstance(src, PostgresAgentEvalSource)


# --------------------------------------------------------------------------- #
# M3 — additive RunRecord fields                                               #
# --------------------------------------------------------------------------- #
async def test_run_record_has_additive_fields_with_safe_defaults():
    rec = RunRecord(
        run_id="r",
        thread_id="t",
        tenant_id=None,
        agent_id=None,
        intent="i",
        status="completed",
        outcome="ok",
        steps=[],
        errors=[],
        created_at=_BASE.isoformat(),
    )
    assert rec.plan_source == "fallback"
    assert rec.skills_used == []


async def test_run_task_populates_the_additive_fields():
    from forgeflow.experience.context_builder import ContextBundle
    import forgeflow.experience.context_builder as cb
    from forgeflow.runtime.orchestrator import RequestContext, TaskCreate, run_task

    bundle = ContextBundle(
        sections=[{"source": "skill", "ref_id": "skill-9", "text": "x"}],
        tokens_used=10,
        tokens_raw=20,
        compression_ratio=0.5,
        hit_rate=0.5,
        recalled=2,
    )

    async def _stub(*args, **kwargs):
        return bundle

    monkeypatch_stub = cb.build_context
    cb.build_context = _stub
    try:
        ctx = RequestContext(tenant_id="t-eval", user_id="admin-1", role="admin")
        handle = await run_task(TaskCreate(intent="分析数据"), ctx, bus=_NullBus())
    finally:
        cb.build_context = monkeypatch_stub

    record = get_run_store().get(handle.run_id)
    assert record is not None
    assert record.skills_used == ["skill-9"]
    # The deterministic path carries no plan meta → honestly "fallback".
    assert record.plan_source == "fallback"


class _NullBus:
    async def emit(self, run_id: str, event_type: str, data: dict) -> None:  # noqa: D401
        return None
