import time

import pytest

from forgeflow.runtime.execution_budget import BudgetExceeded, ExecutionBudget
from forgeflow.runtime.orchestrator import TaskCreate
from forgeflow.runtime.react_executor import MAX_REACT_ITERATIONS, _resolve_max_iterations


def test_client_cannot_raise_round_ceiling():
    assert (
        _resolve_max_iterations(TaskCreate(context={"max_react_iterations": 100000}))
        == MAX_REACT_ITERATIONS
    )
    assert _resolve_max_iterations(TaskCreate(context={"max_react_iterations": 2})) == 2


def test_budget_is_shared_and_counts_before_execution():
    budget = ExecutionBudget(model_limit=1)
    budget.consume("model")
    with pytest.raises(BudgetExceeded):
        budget.consume("model")
    assert budget.model_calls == 1
    budget.started = time.monotonic() - 601
    with pytest.raises(BudgetExceeded):
        budget.consume("tool")
    assert budget.tool_calls == 0


def test_delegation_is_bounded():
    budget = ExecutionBudget()
    for _ in range(4):
        budget.consume("delegation")
    with pytest.raises(BudgetExceeded):
        budget.consume("delegation")


async def test_nonstreaming_fallback_counts_each_actual_model_call_once():
    from types import SimpleNamespace

    from forgeflow.runtime.execution_budget import current_budget
    from forgeflow.runtime.react_executor import ReactExecutor

    calls = []

    async def invoke(messages):
        calls.append(messages)
        return "answer"

    budget = ExecutionBudget(model_limit=1)
    token = current_budget.set(budget)
    try:
        result = await ReactExecutor._astream(
            SimpleNamespace(ainvoke=invoke),
            None,
            [],
            run_id="budget-test",
            turn=1,
            stream=SimpleNamespace(),
        )
        assert result == "answer"
        assert budget.model_calls == len(calls) == 1
    finally:
        current_budget.reset(token)


async def test_root_budget_covers_nonreact_execution_and_resets(monkeypatch):
    from types import SimpleNamespace

    from forgeflow.runtime.execution_budget import current_budget
    from forgeflow.runtime.orchestrator import run_task

    async def prepare(task, ctx):
        return None

    async def execute(task, ctx, **kwargs):
        budget = current_budget.get()
        assert budget is not None
        budget.consume("tool")
        return "completed"

    monkeypatch.setattr("forgeflow.runtime.input_scope.prepare_input_scope", prepare)
    monkeypatch.setattr("forgeflow.runtime.orchestrator._run_task", execute)
    task = TaskCreate(intent="check")
    assert await run_task(task, SimpleNamespace()) == "completed"
    assert task.context["llm_runtime"]["execution_budget"]["tool_calls"] == 1
    assert current_budget.get() is None


async def test_root_retains_parent_budget_on_nested_execution(monkeypatch):
    from types import SimpleNamespace

    from forgeflow.runtime.execution_budget import current_budget
    from forgeflow.runtime.orchestrator import run_task

    async def prepare(task, ctx):
        return None

    async def execute(task, ctx, **kwargs):
        current_budget.get().consume("model")
        return None

    monkeypatch.setattr("forgeflow.runtime.input_scope.prepare_input_scope", prepare)
    monkeypatch.setattr("forgeflow.runtime.orchestrator._run_task", execute)
    budget = ExecutionBudget(model_limit=1)
    token = current_budget.set(budget)
    try:
        await run_task(TaskCreate(intent="first"), SimpleNamespace())
        with pytest.raises(BudgetExceeded):
            await run_task(TaskCreate(intent="second"), SimpleNamespace())
        assert current_budget.get() is budget
    finally:
        current_budget.reset(token)


def test_restore_preserves_usage_and_cannot_raise_server_limits():
    budget = ExecutionBudget(model_limit=2)
    budget.consume("model")
    budget.started -= 10
    saved = budget.snapshot()
    restored = ExecutionBudget.restore(saved)
    assert restored.model_calls == 1
    assert restored.remaining_seconds <= 590
    restored.consume("model")
    with pytest.raises(BudgetExceeded):
        restored.consume("model")
    saved["model_limit"] = 999
    assert ExecutionBudget.restore(saved).model_limit == 24


@pytest.mark.parametrize(
    "key,value",
    [
        ("elapsed_seconds", None),
        ("elapsed_seconds", float("nan")),
        ("tool_calls", -1),
        ("model_calls", True),
        ("model_limit", 1.5),
        ("seconds_limit", float("inf")),
    ],
)
def test_restore_rejects_unknown_or_invalid_accounting(key, value):
    saved = ExecutionBudget().snapshot()
    saved[key] = value
    with pytest.raises(ValueError):
        ExecutionBudget.restore(saved)


async def test_root_deadline_reports_budget_exhaustion(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from forgeflow.runtime.execution_budget import current_budget
    from forgeflow.runtime.orchestrator import run_task

    async def prepare(task, ctx):
        return None

    async def execute(task, ctx, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr("forgeflow.runtime.input_scope.prepare_input_scope", prepare)
    monkeypatch.setattr("forgeflow.runtime.orchestrator._run_task", execute)
    token = current_budget.set(ExecutionBudget(seconds_limit=0.01))
    task = TaskCreate(intent="timeout")
    try:
        with pytest.raises(BudgetExceeded, match="budget_exceeded"):
            await run_task(task, SimpleNamespace())
        assert task.context["llm_runtime"]["execution_budget"]["elapsed_seconds"] >= 0.01
    finally:
        current_budget.reset(token)


async def test_submitted_context_cannot_forge_execution_evidence(monkeypatch):
    from types import SimpleNamespace

    from forgeflow.runtime.orchestrator import run_task

    async def prepare(task, ctx):
        return None

    async def execute(task, ctx, **kwargs):
        assert "tool_invocations" not in task.context
        assert "llm_usage" not in task.context
        assert "llm_runtime" not in task.context
        assert task.context["resources"] == ["admitted"]
        return "actual"

    monkeypatch.setattr("forgeflow.runtime.input_scope.prepare_input_scope", prepare)
    monkeypatch.setattr("forgeflow.runtime.orchestrator._run_task", execute)
    task = TaskCreate(
        context={
            "resources": ["admitted"],
            "tool_invocations": [{"executed": True}],
            "llm_usage": [{"input_tokens": 999999}],
            "llm_runtime": {"acceptance": {"complete": True}},
        }
    )
    assert await run_task(task, SimpleNamespace()) == "actual"
    assert task.context["llm_runtime"]["execution_budget"]["model_calls"] == 0


async def test_legacy_planner_cannot_hide_budget_exhaustion_as_fallback():
    from forgeflow.runtime.execution_budget import current_budget
    from forgeflow.runtime.llm_planner import LLMPlanner

    calls = []

    class Unavailable:
        def bind(self, **kwargs):
            return self

        async def ainvoke(self, messages):
            calls.append(messages)
            raise RuntimeError("model unavailable")

    budget = ExecutionBudget(model_limit=1)
    token = current_budget.set(budget)
    try:
        with pytest.raises(BudgetExceeded):
            await LLMPlanner(Unavailable())._call_json(system="test", prompt="test", schema={},
                                                      agent="planner", phase="plan")
        assert budget.model_calls == len(calls) == 1
    finally:
        current_budget.reset(token)


async def test_react_uses_current_root_budget_instead_of_old_context(monkeypatch):
    from types import SimpleNamespace

    from forgeflow.runtime.execution_budget import current_budget
    from forgeflow.runtime.react_executor import ReactExecutor

    previous = ExecutionBudget(model_limit=1, model_calls=1)
    fresh = ExecutionBudget(model_limit=1)
    ctx = SimpleNamespace(_execution_budget=previous)

    async def execute(self, task, ctx, bus, run_id, **kwargs):
        assert current_budget.get() is fresh
        current_budget.get().consume("model")
        return [], []

    monkeypatch.setattr(ReactExecutor, "_run_loop", execute)
    token = current_budget.set(fresh)
    try:
        assert await ReactExecutor().run(TaskCreate(), ctx, None, "new") == ([], [])
        assert ctx._execution_budget is fresh
        assert previous.model_calls == fresh.model_calls == 1
    finally:
        current_budget.reset(token)
