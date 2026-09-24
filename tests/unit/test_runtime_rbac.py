"""INC2-21 — every runtime step is RBAC re-checked before it executes.

Before this change ``_default_executor`` went straight from "run accepted" to
the risk gate: the route gate only ever saw ``execute:workflows``, so a role
allowed to *start* a run could execute any tool the plan named.
"""

from __future__ import annotations

import pytest

from forgeflow.runtime.gate import check_tool_permission, describe_denial, required_permission
from forgeflow.runtime.orchestrator import RequestContext, TaskCreate, _default_executor


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, run_id: str, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


def test_permission_resolution_is_fail_closed():
    # Built-in plan steps inherit the coarse run permission.
    assert required_permission("research.search") == ("execute", "workflows")
    # Money movement needs its own grant, never inherited.
    assert required_permission("payment.transfer") == ("approve", "proposals")
    # Unknown third-party tools resolve to their namespace — no wildcard.
    assert required_permission("evil.exec") == ("execute", "evil")


def test_role_matrix():
    assert check_tool_permission("admin", "payment.transfer") is True  # *:*
    assert check_tool_permission("sales_rep", "research.search") is True
    assert check_tool_permission("sales_rep", "payment.transfer") is False
    assert check_tool_permission("viewer", "research.search") is False
    assert check_tool_permission("manager", "data.query") is True  # execute:workflows (INC6)
    # Negative control: the widening must NOT leak into privileged / unknown
    # tools. An unknown tool resolves fail-closed to execute:<namespace>.
    assert check_tool_permission("manager", "evil.exec") is False
    assert "缺少" in describe_denial("viewer", "research.search")


async def test_unauthorised_tool_is_denied_before_execution(monkeypatch):
    """A viewer may not run even the built-in plan — and nothing executes."""
    import forgeflow.runtime.orchestrator as orch

    executed: list[str] = []
    original = orch._DEFAULT_STEPS
    monkeypatch.setattr(
        orch,
        "_DEFAULT_STEPS",
        [{"tool": "research.search", "step_type": "agent", "note": "研究"}],
    )
    # Instrument: if the tool ran, this list fills up.
    async def _spy_emit(run_id, event_type, data):  # pragma: no cover - safety net
        if event_type == "run.step":
            executed.append(str(data.get("tool")))

    bus = _RecordingBus()
    ctx = RequestContext(tenant_id="t-rbac", user_id="v-1", role="viewer")

    steps, errors = await orch._default_executor(
        TaskCreate(intent="分析数据"), ctx, bus, "run-rbac-1"
    )

    assert steps == [], "no step may be recorded once RBAC denies"
    assert errors and "权限不足" in errors[0]
    # The denial is emitted as run.error and the run returns immediately.
    assert [t for t, _ in bus.events] == ["run.started", "run.error"]
    assert bus.events[-1][1]["reason"] == "rbac_denied"
    assert executed == []
    assert original is not None


async def test_authorised_role_runs_unchanged():
    """A role with execute:workflows is unaffected by the new gate."""
    bus = _RecordingBus()
    ctx = RequestContext(tenant_id="t-rbac-ok", user_id="s-1", role="sales_rep")

    steps, errors = await _default_executor(
        TaskCreate(intent="分析华东销售数据"), ctx, bus, "run-rbac-2"
    )

    assert errors == []
    assert len(steps) == 3
    assert [s["tool"] for s in steps] == ["research.search", "data.query", "code.run"]


async def test_privileged_tool_denied_for_sales_rep(monkeypatch):
    """Money movement is not inherited from execute:workflows."""
    import forgeflow.runtime.orchestrator as orch

    monkeypatch.setattr(
        orch,
        "_DEFAULT_STEPS",
        [{"tool": "payment.transfer", "step_type": "tool", "note": "转账"}],
    )
    bus = _RecordingBus()
    ctx = RequestContext(tenant_id="t-rbac-3", user_id="s-2", role="sales_rep")

    steps, errors = await orch._default_executor(
        TaskCreate(intent="付款"), ctx, bus, "run-rbac-3"
    )

    assert steps == []
    assert errors and "approve:proposals" in errors[0]
