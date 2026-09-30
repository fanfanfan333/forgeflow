"""V6 regression: the runtime must gate in-run tool calls via the PolicyEngine.

Previously ``forgeflow.runtime.**`` never invoked the PolicyEngine (grep: zero
hits), and the entry gate's coarse ``workflows:execute`` pair always classified
as "low" — so a high-risk tool call inside a task ran unchecked. The runtime
now risk-gates every step and halts on a high-risk tool.
"""

from __future__ import annotations

import pytest

from forgeflow.governance.policy_engine import PolicyEngine
from forgeflow.repositories.memory.policy_repo import MemoryPolicyRepository
from forgeflow.runtime.orchestrator import RequestContext, TaskCreate, run_task

pytestmark = pytest.mark.asyncio


class _RecordingBus:
    """Minimal bus stub — run_task only calls ``emit``."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, run_id: str, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


async def test_high_risk_tool_call_is_blocked_and_raises_approval(monkeypatch):
    import forgeflow.runtime.orchestrator as orch

    monkeypatch.setattr(
        orch,
        "_DEFAULT_STEPS",
        [{"tool": "payment.transfer", "step_type": "tool", "note": "发起转账"}],
    )

    tenant = "t-gate-block"
    repo = MemoryPolicyRepository()
    engine = PolicyEngine(repo=repo)
    ctx = RequestContext(tenant_id=tenant, user_id="admin-1", role="admin")
    task = TaskCreate(intent="处理付款", workflow_type="generic")
    bus = _RecordingBus()

    handle = await run_task(task, ctx, bus=bus, policy_engine=engine)

    # The run must fail (blocked), not silently execute the dangerous tool.
    assert handle.status == "failed"
    assert handle.detail["steps"] == []
    assert any("拦截" in e for e in handle.detail["errors"])

    # A HITL approval was created for the high-risk tool.
    approvals = await repo.list_approvals(tenant, status="pending")
    assert len(approvals) >= 1
    assert any("transfer" in a.requested_action for a in approvals)

    # The block surfaced on the event stream.
    assert any(t == "run.error" for t, _ in bus.events)


async def test_low_risk_tools_run_normally():
    tenant = "t-gate-ok"
    repo = MemoryPolicyRepository()
    engine = PolicyEngine(repo=repo)
    ctx = RequestContext(tenant_id=tenant, user_id="admin-1", role="admin")
    task = TaskCreate(intent="分析华东地区销售数据并生成报告", workflow_type="generic")
    bus = _RecordingBus()

    handle = await run_task(task, ctx, bus=bus, policy_engine=engine)

    assert handle.status == "completed"
    # INC15 — the plan is dynamic: a plain intent (no table / paths) plans only
    # the applicable steps, so the run ends on the deliverable, not a fixed 4.
    tools = [s["tool"] for s in handle.detail["steps"]]
    assert tools == ["research.search", "report.render"]
    approvals = await repo.list_approvals(tenant, status="pending")
    assert approvals == []
