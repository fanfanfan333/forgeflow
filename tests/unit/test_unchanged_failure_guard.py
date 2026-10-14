import pytest

from forgeflow.runtime.execution_budget import ExecutionBudget, current_budget
from forgeflow.runtime.tool_executor import ToolCallContext, ToolExecutor
from forgeflow.runtime.tool_registry import ToolBinding


@pytest.mark.asyncio
async def test_same_failure_stops_until_actual_evidence_changes(monkeypatch):
    calls = []
    version = [1]

    async def fail(args, ctx):
        calls.append(ctx.run_id)
        return {"ok": False, "error": "input rejected"}

    async def read(args, ctx):
        return {"ok": True, "version": version[0]}

    bindings = {
        "test.edit": ToolBinding("test.edit", fail, "real", "test", ""),
        "test.read": ToolBinding("test.read", read, "real", "test", ""),
    }
    monkeypatch.setattr("forgeflow.runtime.tool_executor.resolve", bindings.get)
    budget = ExecutionBudget()
    token = current_budget.set(budget)
    executor = ToolExecutor()

    async def invoke(tool, run="first"):
        return await executor.execute(tool, ctx=ToolCallContext(
            run_id=run, step_id="step", tenant_id="t", user_id="u", role="admin", args={}
        ))

    try:
        await invoke("test.read")
        for _ in range(2):
            assert (await invoke("test.edit")).invoked
        blocked = await invoke("test.edit")
        assert blocked.status == "error" and not blocked.invoked
        assert blocked.payload["repeat_failure"]
        await invoke("test.read")
        assert not (await invoke("test.edit")).invoked
        version[0] = 2
        await invoke("test.read")
        assert (await invoke("test.edit")).invoked
        assert (await invoke("test.edit", run="other")).invoked
        assert calls == ["first", "first", "first", "other"]
    finally:
        current_budget.reset(token)
