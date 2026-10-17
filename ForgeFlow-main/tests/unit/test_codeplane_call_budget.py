
import pytest

from forgeflow.codeplane.runner.run_code_task import _CallBudget, _CallBudgetExceeded
from forgeflow.runtime.execution_budget import BudgetExceeded, ExecutionBudget


def test_root_reservations_prevent_unaccounted_parallel_spend():
    budget = ExecutionBudget(model_limit=3, tool_limit=5)
    budget.consume("model")
    lease = budget.reserve_code()
    assert lease == {"model": 2, "tool": 5}
    with pytest.raises(BudgetExceeded):
        budget.consume("model")
    budget.settle_code(lease, {"model": 1, "tool": 2})
    assert budget.model_calls == 2 and budget.tool_calls == 2
    budget.consume("model")
    with pytest.raises(BudgetExceeded):
        budget.consume("model")


def test_unknown_usage_keeps_reservation_after_restart():
    budget = ExecutionBudget()
    lease = budget.reserve_code()
    with pytest.raises(ValueError):
        budget.settle_code(lease, {"model": -1, "tool": 0})
    restored = ExecutionBudget.restore(budget.snapshot())
    with pytest.raises(BudgetExceeded):
        restored.consume("model")


def test_actual_provider_attempt_is_counted_before_side_effect():
    budget = _CallBudget({"model": 1, "tool": 1})
    calls = []
    wrapped = budget.wrap_sync(lambda **kw: calls.append(kw), "model")
    wrapped(num_retries=99)
    with pytest.raises(_CallBudgetExceeded):
        wrapped()
    assert calls == [{"num_retries": 0}]
    assert budget.usage == {"model": 1, "tool": 0}


async def test_async_provider_and_tools_share_same_counter():
    budget = _CallBudget({"model": 1, "tool": 1})
    async def provider(**kwargs):
        return kwargs
    assert await budget.wrap_async(provider)() == {"num_retries": 0}
    with pytest.raises(_CallBudgetExceeded):
        await budget.wrap_async(provider)()
    assert budget.wrap_sync(lambda: "done", "tool")() == "done"
    with pytest.raises(_CallBudgetExceeded):
        budget.wrap_sync(lambda: "never", "tool")()


def test_model_guards_are_thread_scoped_and_not_wrapped_twice(monkeypatch):
    import importlib
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace

    async def async_provider(**kwargs):
        return kwargs
    module = SimpleNamespace(litellm_completion=lambda **kwargs: kwargs, litellm_responses=lambda **kwargs: kwargs,
                             litellm_acompletion=async_provider, litellm_aresponses=async_provider)
    original = importlib.import_module
    monkeypatch.setattr(importlib, "import_module", lambda name, *args: module if name == "openhands.sdk.llm.llm" else original(name, *args))
    parent = _CallBudget({"model": 1, "tool": 1})
    parent.install_models()
    assert module.litellm_completion() == {"num_retries": 0}
    def other_conversation():
        child = _CallBudget({"model": 2, "tool": 1})
        child.install_models()
        module.litellm_completion()
        module.litellm_completion()
        return child.usage
    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(other_conversation).result() == {"model": 2, "tool": 0}
    with pytest.raises(_CallBudgetExceeded):
        module.litellm_completion()
    assert parent.usage == {"model": 1, "tool": 0}


@pytest.mark.parametrize("base,trust", [("http://127.0.0.1:1234", False), ("http://[::1]:1234", False), ("https://agent.example", True)])
def test_agentserver_health_proxy_scope(monkeypatch, base, trust):
    from types import SimpleNamespace

    import httpx

    from forgeflow.codeplane.engine import AgentServerOpenHandsEngine

    seen = []
    def get(url, **kwargs):
        seen.append(kwargs)
        return SimpleNamespace(status_code=200)
    monkeypatch.setattr(httpx, "get", get)
    engine = AgentServerOpenHandsEngine(settings=SimpleNamespace())
    assert engine._health_get(base, "synthetic-token")[0]
    assert seen[0]["trust_env"] is trust


def test_agentserver_preserves_provider_reported_token_usage(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from forgeflow.codeplane.runner.agent_server.conversation import ConversationRuntime

    metrics = SimpleNamespace(token_usages=[object()], accumulated_token_usage=SimpleNamespace(prompt_tokens=17, completion_tokens=3))
    conversation = SimpleNamespace(agent=SimpleNamespace(llm=SimpleNamespace(model="fixture-model", metrics=metrics)),
                                   state=SimpleNamespace(events=[]), run=lambda: None)
    monkeypatch.setattr(ConversationRuntime, "_build", lambda self: conversation)
    request = SimpleNamespace(agent={}, workspace={"working_dir": str(tmp_path)}, max_iterations=1)
    runtime = ConversationRuntime("fixture", request)
    runtime._drive()
    usage = next(frame["usage"] for frame in runtime.frames_after(-1) if frame.get("type") == "ForgeFlowTokenUsage")
    assert usage == {"input_tokens": 17, "output_tokens": 3, "model": "fixture-model"}
    assert runtime.execution_status == "finished"



def test_code_budget_settlement_cannot_release_a_reservation_twice():
    budget = ExecutionBudget(model_limit=2, tool_limit=3)
    lease = budget.reserve_code()
    budget.settle_code(lease, {"model": 1, "tool": 1})
    before = budget.snapshot()
    with pytest.raises(ValueError, match="already settled"):
        budget.settle_code(lease, {"model": 0, "tool": 0})
    after = budget.snapshot()
    for field in ("model_calls", "tool_calls", "reserved_model_calls", "reserved_tool_calls"):
        assert after[field] == before[field]


@pytest.mark.parametrize("usage", [{"model": 0, "tool": True}, {"model": 0, "tool": 4}])
def test_invalid_settlement_is_atomic(usage):
    budget = ExecutionBudget(model_limit=2, tool_limit=3)
    lease = budget.reserve_code()
    with pytest.raises(ValueError):
        budget.settle_code(lease, usage)
    assert budget.reserved_model_calls == 2 and budget.reserved_tool_calls == 3
    assert budget.model_calls == 0 and budget.tool_calls == 0



def test_old_lease_cannot_settle_a_new_identical_reservation():
    budget = ExecutionBudget(model_limit=2, tool_limit=3)
    old = budget.reserve_code()
    budget.settle_code(old, {"model": 0, "tool": 0})
    current = budget.reserve_code()
    assert current == old
    with pytest.raises(ValueError, match="already settled"):
        budget.settle_code(old, {"model": 0, "tool": 0})
    budget.settle_code(current, {"model": 1, "tool": 1})
    assert budget.model_calls == budget.tool_calls == 1
