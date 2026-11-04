"""Phase 9 B3 — depth-capped nested delegation + delegation tree.

The old blanket ban ("no nested delegation at all") is replaced by a
depth cap: root (0) → child (1) → grandchild (2, may not delegate
further). The tree is readable from the parent run's record: each
delegation meta nests its own children, with true (not "running")
statuses.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from forgeflow.runtime import delegation as delegation_mod
from forgeflow.runtime.delegation import (
    MAX_DELEGATION_DEPTH,
    _child_allowed_tools,
    _delegation_depth,
    _validate_delegation_item,
    delegate,
    delegate_parallel,
)
from forgeflow.runtime.execution_budget import ExecutionBudget, current_budget


def _ctx(depth: int | None = None):
    context: dict = {"resources": [], "llm_runtime": {}}
    if depth is not None:
        context["_delegation_depth"] = depth
    parent = SimpleNamespace(delegation_role=None, allowed_tools=None)
    task = SimpleNamespace(context=context)
    return SimpleNamespace(
        runtime_context=parent,
        task_context=task,
        tenant_id="t",
        user_id="u",
        role="admin",
        run_id="p",
    )


def test_depth_defaults_to_zero_and_reads_context() -> None:
    assert _delegation_depth(_ctx()) == 0
    assert _delegation_depth(_ctx(1)) == 1
    assert _delegation_depth(_ctx("deep")) == 0


def test_child_allowlist_gates_agent_delegate_on_depth() -> None:
    parent = SimpleNamespace(allowed_tools=None)
    assert "agent.delegate" in _child_allowed_tools(parent, "research", 1)
    assert "agent.delegate" not in _child_allowed_tools(parent, "research", 2)
    assert "agent.delegate" not in _child_allowed_tools(parent, "research", 99)
    assert "research.search" in _child_allowed_tools(parent, "research", 2)


def test_delegate_at_max_depth_returns_skipped_not_raises() -> None:
    ctx = _ctx(MAX_DELEGATION_DEPTH)
    budget = ExecutionBudget()
    token = current_budget.set(budget)
    try:
        result = asyncio.run(delegate({"role": "research", "instruction": "review"}, ctx))
    finally:
        current_budget.reset(token)
    assert result["ok"] is False
    assert result["status"] == "skipped"
    assert result["delegation_run_id"] is None
    assert "depth limit" in result["error"]
    assert budget.delegations == 0


def test_delegate_below_max_passes_the_gate() -> None:
    # No budget set: the gate passed (depth ok) and the budget check
    # is what stops it — proving depth did not refuse first.
    ctx = _ctx(1)
    with pytest.raises(PermissionError, match="server-owned budget"):
        asyncio.run(delegate({"role": "research", "instruction": "review"}, ctx))


def test_validate_at_max_depth_fails_only_that_item() -> None:
    ctx = _ctx(MAX_DELEGATION_DEPTH)
    with pytest.raises(PermissionError, match="depth limit"):
        _validate_delegation_item({"role": "research", "instruction": "review"}, ctx)


@pytest.mark.asyncio
async def test_parallel_rebuild_carries_delegations(monkeypatch) -> None:
    async def fake_delegate(item, ctx):
        return {
            "ok": True,
            "delegation_run_id": "run-child",
            "status": "completed",
            "summary": "ok",
            "summary_truncated": False,
            "artifacts": [],
            "total_tokens": 0,
            "resources_dropped": [],
            "requirement_ids_dropped": [],
            "evidence": [],
            "delegations": [{"run_id": "run-grand", "role": "code_review", "status": "completed"}],
            "error": None,
        }

    monkeypatch.setattr(delegation_mod, "delegate", fake_delegate)
    budget = ExecutionBudget()
    token = current_budget.set(budget)
    try:
        result = await delegate_parallel(
            {"delegations": [{"role": "research", "instruction": "review", "resources": [], "requirement_ids": []}]},
            _ctx(),
        )
    finally:
        current_budget.reset(token)
    assert result["results"][0]["delegations"][0]["run_id"] == "run-grand"


@pytest.mark.asyncio
async def test_child_run_records_depth_allowlist_and_tree(monkeypatch) -> None:
    """A stubbed child run: depth 1, agent.delegate allowed, and the
    grandchild meta it reports nests under the parent's delegation meta."""
    from forgeflow.runtime.orchestrator import RunRecord

    captured: dict = {}

    class _FakeExecutor:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, task, child, bus, run_id):
            captured["depth"] = task.context.get("_delegation_depth")
            captured["tools"] = set(getattr(child, "allowed_tools", set()))
            task.context.setdefault("llm_runtime", {})["delegations"] = [
                {"run_id": "run-grand", "role": "code_review", "status": "completed", "delegations": []}
            ]
            task.context["llm_runtime"]["final_answer"] = {"text": "child done"}
            task.context["tool_invocations"] = [
                {"tool": "research.search", "status": "ok", "executed": True, "payload": {}}
            ]
            return [], []

    class _FakeBus:
        async def emit(self, *args, **kwargs):
            return None

    class _FakeStore:
        async def save(self, *args, **kwargs):
            return None

    async def _fake_scope(task, child):
        return None

    def _fake_register(**kwargs):
        return RunRecord(
            run_id=kwargs["run_id"],
            thread_id=kwargs["run_id"],
            tenant_id=kwargs.get("tenant_id"),
            agent_id=None,
            intent=kwargs.get("intent", ""),
            status="running",
            outcome="",
            steps=[],
            errors=[],
            created_at="2026-11-04T00:00:00+00:00",
            tool_invocations=[],
        )

    import forgeflow.runtime.events as events_mod
    import forgeflow.runtime.input_scope as scope_mod
    import forgeflow.runtime.orchestrator as orch_mod
    import forgeflow.runtime.react_executor as react_mod
    import forgeflow.workspace.store as ws_mod

    monkeypatch.setattr(events_mod, "get_event_bus", lambda: _FakeBus())
    monkeypatch.setattr(orch_mod, "register_running_run", _fake_register)
    monkeypatch.setattr(ws_mod, "get_workspace_store", lambda: _FakeStore())
    monkeypatch.setattr(scope_mod, "prepare_input_scope", _fake_scope)
    monkeypatch.setattr(react_mod, "ReactExecutor", _FakeExecutor)

    ctx = _ctx()
    budget = ExecutionBudget()
    token = current_budget.set(budget)
    try:
        result = await delegate({"role": "research", "instruction": "review"}, ctx)
    finally:
        current_budget.reset(token)
    assert result["ok"] is True
    assert captured["depth"] == 1
    assert "agent.delegate" in captured["tools"]
    assert result["delegations"][0]["run_id"] == "run-grand"
    metas = ctx.task_context.context["llm_runtime"]["delegations"]
    assert metas[0]["status"] == "completed"
    assert metas[0]["delegations"][0]["run_id"] == "run-grand"
