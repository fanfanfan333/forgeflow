"""FF-R1 B4 (FF-G21) — a run that used a development stub must not
report ``outcome=success``.

On the offline profile (deterministic mode, mock provider, no Tavily)
``research.search`` resolves to the development stub: it returns
simulated rows, honestly flagged ``development_stub`` in the payload
and stamped on the invocation by the ToolExecutor. The run completes
and renders a report, but its research never touched a real source —
reporting ``success`` overstates the delivery. Such a run's outcome
is ``degraded`` (the run still completes; only the label changes).
"""

from __future__ import annotations

import pytest

from forgeflow.runtime.orchestrator import (
    RequestContext,
    TaskCreate,
    get_run_store,
    reset_run_store,
    run_task,
)

pytestmark = pytest.mark.asyncio


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, run_id: str, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


@pytest.fixture(autouse=True)
def _clean_state():
    reset_run_store()
    yield
    reset_run_store()


async def test_deterministic_run_using_research_stub_reports_degraded(
    force_memory_backend,
):
    task = TaskCreate(
        intent="检索 forgeflow 并总结",
        workflow_type="generic",
        context={"query": "forgeflow"},
    )
    handle = await run_task(
        task,
        RequestContext(tenant_id="t-g21", user_id="u-1", role="admin"),
        bus=_RecordingBus(),
    )
    record = get_run_store().get(handle.run_id)
    assert record is not None
    # The stub really ran (this is the precondition of the defect).
    stub_invocations = [
        inv
        for inv in (task.context.get("tool_invocations") or [])
        if inv.get("tool") == "research.search" and inv.get("executed")
    ]
    assert stub_invocations, "research.search should have executed via the stub"
    assert any(inv.get("development_stub") for inv in stub_invocations)
    # ... so the run must not claim success.
    assert handle.status == "completed"
    assert record.outcome == "degraded"
    assert record.verdict["outcome"] == "degraded"
    # The downgrade is a label change only (like ``partial``): the
    # success flag — which drives replan / HITL — stays True, and the
    # verdict's reasons say WHY, naming the development stub.
    assert record.verdict["success"] is True
    assert any("development stub" in str(reason) for reason in record.verdict["reasons"]), (
        record.verdict["reasons"]
    )


async def test_blocked_stub_invocation_does_not_degrade_the_outcome(
    force_memory_backend, monkeypatch
):
    """Negative case: an invocation can carry the development-stub
    stamp without any simulated output being consumed — here
    research.search is blocked before execution (no Tavily and a
    non-mock provider, so the handler refuses to use simulated
    results). Nothing was consumed, so the outcome must NOT be
    degraded; it keeps its natural verdict."""
    import forgeflow.config as config_module
    import forgeflow.runtime.orchestrator as orch
    from forgeflow.config import get_settings

    real = get_settings()

    class _Proxy:
        def __getattr__(self, item: str):
            if item == "llm_provider":
                return "ollama"
            return getattr(real, item)

        def __setattr__(self, key: str, value) -> None:
            setattr(real, key, value)

    monkeypatch.setattr(orch, "get_settings", lambda: _Proxy())
    monkeypatch.setattr(config_module, "get_settings", lambda: _Proxy())

    task = TaskCreate(
        intent="检索 forgeflow 并总结",
        workflow_type="generic",
        context={"query": "forgeflow"},
    )
    handle = await run_task(
        task,
        RequestContext(tenant_id="t-g21-neg", user_id="u-1", role="admin"),
        bus=_RecordingBus(),
    )
    record = get_run_store().get(handle.run_id)
    assert record is not None
    blocked_stub = [
        inv
        for inv in (task.context.get("tool_invocations") or [])
        if inv.get("tool") == "research.search" and inv.get("development_stub")
    ]
    assert blocked_stub, "the stub-stamped invocation should exist"
    assert not any(inv.get("executed") for inv in blocked_stub), (
        "precondition: the stub invocation must not have executed"
    )
    assert record.outcome != "degraded"
    assert not any("development stub" in str(reason) for reason in record.verdict["reasons"])
