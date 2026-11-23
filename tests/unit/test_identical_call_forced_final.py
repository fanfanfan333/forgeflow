"""FF-R1 B3 (FF-G9) — the identical-call guard must actually stop the run.

Diagnosis (measured on main c227cb0 with this very drive): a model
that proposes the same (tool, normalized args) call forever burned
**13 model calls** across 3 attempts and still failed. The
proposal-side guard (``_is_third_identical_call``) DID fire — a
dead_loop warning per attempt, after exactly two executions — but
its signature history is rebuilt empty for every attempt, so each
replan handed the model two more identical executions, and the
break-with-error ending invited the next replan.

The fix, per the B3 brief: the third identical proposal forces the
final-answer turn (the calls are dropped, the model answers from the
evidence already collected), and the signature history is run-scoped
so a streak survives replans. This drive must now close in at most
4 model calls: propose, propose, third proposal dropped, final
answer.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

import forgeflow.config as config_module
import forgeflow.runtime.orchestrator as orch
import forgeflow.runtime.react_executor as react_module
from forgeflow.config import get_settings
from forgeflow.runtime.orchestrator import (
    RequestContext,
    TaskCreate,
    reset_run_store,
    run_task,
)
from forgeflow.runtime.tool_registry import (
    ToolBinding,
    load_default_bindings,
    register,
    reset_registry,
)

pytestmark = pytest.mark.asyncio


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, run_id: str, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


class _SettingsProxy:
    def __init__(self, real, *, mode: str, provider: str) -> None:
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_mode", mode)
        object.__setattr__(self, "_provider", provider)

    def __getattr__(self, item: str):
        if item == "agent_runtime_mode":
            return self._mode
        if item == "llm_provider":
            return self._provider
        return getattr(self._real, item)

    def __setattr__(self, key: str, value) -> None:
        setattr(self._real, key, value)


class _RepeatingModel:
    """Proposes the identical search call until the loop's final-answer
    nudge arrives, then answers — a cooperative real model, scripted."""

    def __init__(self) -> None:
        self.model = "scripted-model"
        self._llm_type = "ollama"
        self.calls = 0

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        return self

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        last_human = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
        if last_human is not None and "最终答复" in str(last_human.content):
            return AIMessage(content="已根据已有检索结果给出最终答复。")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "research.search",
                    "args": {"query": "forgeflow"},
                    "id": "dup",
                    "type": "tool_call",
                }
            ],
        )


@pytest.fixture(autouse=True)
def _registry_isolation():
    reset_registry()
    load_default_bindings()
    yield
    reset_registry()
    load_default_bindings()


async def test_identical_calls_force_the_final_answer_within_four_model_calls(
    monkeypatch,
):
    executions = {"n": 0}

    async def _fake_search(args, ctx):  # noqa: ANN001, ANN202
        executions["n"] += 1
        return {"ok": True, "results": [{"title": "forgeflow"}]}

    register(
        ToolBinding(
            tool_id="research.search",
            handler=_fake_search,
            kind="real",
            provider="test",
            description="fake search",
        )
    )
    model = _RepeatingModel()
    proxy = _SettingsProxy(get_settings(), mode="react", provider="ollama")
    monkeypatch.setattr(orch, "get_settings", lambda: proxy)
    monkeypatch.setattr(config_module, "get_settings", lambda: proxy)
    info = {"slot": "strong", "class": "S", "llm_type": "ollama", "model": model.model}
    monkeypatch.setattr(orch, "_build_planner_models", lambda: (model, model, [info]))
    monkeypatch.setattr(react_module, "backoff_delay", lambda *a, **k: 0.0)
    reset_run_store()
    bus = _RecordingBus()
    task = TaskCreate(intent="检索并总结 forgeflow", workflow_type="generic", context={})
    handle = await run_task(
        task,
        RequestContext(tenant_id="t-b3", user_id="u-1", role="admin"),
        bus=bus,
    )

    # The third identical proposal is dropped and the loop closes with
    # a forced final answer — no replan churn (13 calls before the fix).
    assert model.calls <= 4, f"expected <= 4 model calls, got {model.calls}"
    assert executions["n"] == 2, "the third identical call must not execute"
    assert handle.status == "completed"
    meta = task.context.get("llm_runtime") or {}
    assert meta.get("repeated_tool_call_forced_final") is True
    # The streak is run-scoped now: the signature history persists on
    # the task so a later attempt would see it too.
    history = task.context.get("_identical_call_signatures") or []
    assert len(history) >= 2
