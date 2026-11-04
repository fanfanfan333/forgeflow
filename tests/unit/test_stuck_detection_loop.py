"""Stuck detection wired into the real react loop (not just the pure detector).

The pure patterns live in ``test_stuck_detection.py``. These tests prove
the loop itself warns once per pattern and forces a final answer when
the same pattern is still present a full turn later, using the same
``run_task`` harness shape as ``test_inc17_executor_only.py``.
"""

import pytest
from langchain_core.messages import AIMessage

import forgeflow.runtime.orchestrator as orch
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


class _ScriptedBound:
    def __init__(self, model: "_ScriptedModel") -> None:
        self._model = model

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        return self._model._next(messages)


class _ScriptedModel:
    def __init__(self, script: list[dict]) -> None:
        self._script = list(script)
        self._last: dict = {"content": "", "tool_calls": []}
        self.model = "fake-react-model"
        self._llm_type = "fake"

    def bind_tools(self, tools):  # noqa: ANN001
        return _ScriptedBound(self)

    def bind(self, **kwargs):  # noqa: ANN003
        return _ScriptedBound(self)

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        return self._next(messages)

    def _next(self, messages) -> AIMessage:  # noqa: ANN001
        if self._script:
            self._last = self._script.pop(0)
        return AIMessage(
            content=self._last.get("content", ""),
            tool_calls=self._last.get("tool_calls", []),
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )


async def _failing_search(args, ctx):  # noqa: ANN001
    return {"ok": False, "error": "timeout", "summary": "timeout"}


@pytest.fixture
def failing_search():
    load_default_bindings()
    register(
        ToolBinding(
            tool_id="research.search",
            handler=_failing_search,
            kind="real",
            provider="test",
            description="scripted always-failing search",
        )
    )
    yield
    reset_registry()


class _ProxySettings:
    """Settings view forcing the react mode (same shape as the inc17 test)."""

    def __init__(self) -> None:
        self._real = get_settings()

    @property
    def agent_runtime_mode(self) -> str:
        return "react"

    @property
    def llm_provider(self) -> str:
        return "mock"

    def __getattr__(self, item: str):
        return getattr(self._real, item)


def _search_call(call_id: str, query: str) -> dict:
    return {
        "name": "research.search",
        "args": {"query": query},
        "id": call_id,
    }


async def _run_scripted(monkeypatch, script: list[dict]):
    reset_run_store()
    fake = _ScriptedModel(script)
    monkeypatch.setattr(orch, "get_settings", lambda: _ProxySettings())
    monkeypatch.setattr(
        orch,
        "_build_planner_models",
        lambda: (fake, fake, [{"slot": "strong", "class": "_ScriptedModel", "model": fake.model}]),
    )
    task = TaskCreate(intent="测试循环", workflow_type="generic")
    handle = await run_task(
        task, RequestContext(tenant_id="t-d", user_id="u", role="admin"), bus=_RecordingBus()
    )
    reset_registry()
    return task, handle


async def test_loop_warns_on_same_error_with_varying_args(
    monkeypatch, force_memory_backend, failing_search
):
    task, handle = await _run_scripted(
        monkeypatch,
        [
            {"content": "查1", "tool_calls": [_search_call("c1", "a")]},
            {"content": "查2", "tool_calls": [_search_call("c2", "b")]},
            # Phase 3 planning interleaves after two stall rounds: it
            # consumes one model call but adds no tool record, so the
            # stuck trail still reaches three failures below.
            {"content": "1. 换个查法", "tool_calls": []},
            {"content": "查3", "tool_calls": [_search_call("c3", "c")]},
            {"content": "已收到足够信息，最终答复", "tool_calls": []},
        ],
    )
    meta = task.context["llm_runtime"]
    detected = meta.get("stuck_patterns_detected", [])
    assert any(p["key"].startswith("same_error:research.search:") for p in detected), detected
    records = handle.detail["tool_invocations"]
    assert sum(1 for r in records if r["tool"] == "research.search") == 3


async def test_loop_forces_final_when_pattern_persists(
    monkeypatch, force_memory_backend, failing_search
):
    # The model never stops calling the failing tool (varying args, so
    # the identical-call guard cannot see it — that guard only stops
    # a third *identical* call). The stuck guard must stop it after
    # one warned turn: 4 executions total, then the fifth turn's call
    # is dropped and the run closes with the model's own text instead
    # of burning the whole model budget.
    task, handle = await _run_scripted(
        monkeypatch,
        [
            {"content": "再查", "tool_calls": [_search_call("c1", "q1")]},
            {"content": "再查", "tool_calls": [_search_call("c2", "q2")]},
            {"content": "再查", "tool_calls": [_search_call("c3", "q3")]},
            {"content": "再查", "tool_calls": [_search_call("c4", "q4")]},
            {"content": "再查", "tool_calls": [_search_call("c5", "q5")]},
            {"content": "再查", "tool_calls": [_search_call("c6", "q6")]},
        ],
    )
    meta = task.context["llm_runtime"]
    assert meta.get("stuck_forced_final") is True
    records = handle.detail["tool_invocations"]
    assert sum(1 for r in records if r["tool"] == "research.search") == 4, records
