"""INC2-10 — ``build_context`` is genuinely wired into the runtime (not dead code).

The audit finding was that ``experience/context_builder.build_context`` had no
caller anywhere in the repo. These tests pin the call site so it cannot silently
rot back into dead code, and cover the A1↔A5 budget link plus the stats sink.
"""

from __future__ import annotations

import pytest

from forgeflow.experience.context_builder import ContextBundle, build_context
from forgeflow.observability.context_stats import get_build_stats, record_context_build, reset_build_stats
from forgeflow.runtime.orchestrator import RequestContext, TaskCreate, run_task

pytestmark = pytest.mark.asyncio


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, run_id: str, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


async def test_run_task_calls_build_context(monkeypatch):
    """The runtime really invokes the builder — spy on the real function."""
    import forgeflow.experience.context_builder as cb

    calls: list[int] = []

    async def _spy(*args, **kwargs):
        calls.append(1)
        return await build_context(*args, **kwargs)

    monkeypatch.setattr(cb, "build_context", _spy)

    ctx = RequestContext(tenant_id="t-ctx-1", user_id="admin-1", role="admin")
    handle = await run_task(TaskCreate(intent="分析华东销售数据"), ctx, bus=_RecordingBus())

    assert handle.status == "completed"
    assert len(calls) == 1, "build_context must be called exactly once per run"


async def test_selected_skills_become_available_skills(monkeypatch):
    """``ctx.available_skills`` is filled from the bundle's skill sections."""
    import forgeflow.experience.context_builder as cb

    bundle = ContextBundle(
        sections=[
            {"source": "skill", "ref_id": "skill-42", "text": "销售分析技能"},
            {"source": "memory", "ref_id": "mem-1", "text": "历史偏好"},
        ],
        tokens_used=120,
        tokens_raw=300,
        compression_ratio=0.4,
        hit_rate=0.5,
        recalled=4,
    )

    async def _stub(*args, **kwargs):
        return bundle

    monkeypatch.setattr(cb, "build_context", _stub)

    ctx = RequestContext(tenant_id="t-ctx-2", user_id="admin-1", role="admin")
    await run_task(TaskCreate(intent="分析数据"), ctx, bus=_RecordingBus())

    assert ctx.available_skills == ["skill-42"]


async def test_context_build_stats_are_recorded():
    reset_build_stats()
    bundle = ContextBundle(
        sections=[{"source": "memory", "ref_id": "m1", "text": "x"}],
        tokens_used=50,
        tokens_raw=200,
        compression_ratio=0.25,
        hit_rate=0.25,
        recalled=4,
    )
    entry = record_context_build(bundle, tenant_id="t-stats", run_id="r-1")
    stats = get_build_stats()

    assert entry.compression_ratio == pytest.approx(0.25)
    assert stats["builds"] == 1
    assert stats["has_data"] is True
    assert stats["compression_ratio"] == pytest.approx(0.25)
    assert stats["hit_rate"] == pytest.approx(0.25)
    reset_build_stats()
    assert get_build_stats()["has_data"] is False


async def test_trim_context_halves_the_budget(monkeypatch):
    """A1 ``trim_context`` → the A5 context budget drops by 50%."""
    from forgeflow.cost.budget_service import BudgetService
    from forgeflow.cost.degrade import build_degrade_state
    from forgeflow.runtime.orchestrator import _resolve_context_budget

    ctx = RequestContext(tenant_id="t-ctx-3", user_id="admin-1", role="admin")

    # Baseline: no degrade action → full configured budget.
    monkeypatch.setattr(BudgetService, "evaluate_budget", _decision([]))
    full = await _resolve_context_budget(TaskCreate(intent="x"), ctx)

    # With trim_context in effect → half.
    monkeypatch.setattr(BudgetService, "evaluate_budget", _decision(["trim_context"]))
    trimmed = await _resolve_context_budget(TaskCreate(intent="x"), ctx)

    assert full == 2000
    assert trimmed == 1000
    assert build_degrade_state(["trim_context"]).context_budget_multiplier == pytest.approx(0.5)
    assert build_degrade_state(["trim_context"]).effective_context_budget(2000) == 1000


class _Decision:
    """Minimal BudgetDecision stand-in carrying only the actions list."""

    def __init__(self, actions: list[str]) -> None:
        self.actions = actions


def _decision(actions: list[str]):
    """Return an async ``evaluate_budget`` replacement yielding ``actions``."""

    async def _inner(self, *args, **kwargs):
        return _Decision(actions)

    return _inner
