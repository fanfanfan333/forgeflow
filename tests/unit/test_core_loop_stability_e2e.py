"""Core-loop stability — end-to-end through the real react loop.

Drives ``run_task`` with a scripted model + fake tools (the INC17
harness pattern) and pins the phase-1 safeguards:

(a) a tool raising an exception → the loop survives and the model
    receives a *structured* error observation;
(b) a tool timing out → loop-level timeout outcome, retried per
    policy, loop answers cleanly;
(c) a dead loop (same tool + same args, repeatedly) → the loop stops
    with ``terminated_by == "dead_loop"``;
(d) oversized tool output → the model receives truncated feedback
    with the truncation marker while the record keeps the full
    payload;
(e) the token budget cap stops the loop (``token_budget``);
(f) consecutive tool errors stop the loop (``repeated_errors``).

Retry delays are shrunk through task-context policy overrides, so
nothing here really sleeps (except the deliberately slow handler in
(b), which is cut off by the step timeout).
"""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage

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

_FAST_RETRY = {
    "retry_initial_backoff_seconds": 0.001,
    "retry_max_backoff_seconds": 0.002,
    "retry_jitter_seconds": 0.0,
}


# --------------------------------------------------------------------------- #
# Harness (mirrors tests/unit/test_inc17_react_loop.py)                        #
# --------------------------------------------------------------------------- #
class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, run_id: str, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


class _ScriptedBound:
    def __init__(self, model: _ScriptedModel) -> None:
        self._model = model

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        return self._model._next(messages)


class _ScriptedModel:
    def __init__(self, script: list[dict], usage: tuple[int, int] = (10, 5)) -> None:
        self._script = list(script)
        self._usage = usage
        self._last: dict = {"content": "", "tool_calls": []}
        self.calls: list[list] = []
        self.model = "fake-react-model"
        self._llm_type = "fake"

    def bind_tools(self, tools):  # noqa: ANN001
        return _ScriptedBound(self)

    def bind(self, **kwargs):  # noqa: ANN003
        return _ScriptedBound(self)

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        return self._next(messages)

    def _next(self, messages) -> AIMessage:  # noqa: ANN001
        self.calls.append(list(messages))
        if self._script:
            self._last = self._script.pop(0)
        return AIMessage(
            content=self._last.get("content", ""),
            tool_calls=self._last.get("tool_calls", []),
            usage_metadata={
                "input_tokens": self._usage[0],
                "output_tokens": self._usage[1],
                "total_tokens": sum(self._usage),
            },
        )


class _SettingsProxy:
    def __init__(self, real, *, mode="react", provider="mock") -> None:
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_mode", mode)
        object.__setattr__(self, "_provider", provider)

    def __getattr__(self, item: str):
        if item == "agent_runtime_mode":
            return self._mode
        if item == "llm_provider":
            return self._provider
        return getattr(self._real, item)


@pytest.fixture(autouse=True)
def _clean_state():
    reset_run_store()
    yield
    reset_run_store()
    reset_registry()


def _register(tool_id: str, handler) -> None:
    load_default_bindings()
    register(
        ToolBinding(
            tool_id=tool_id,
            handler=handler,
            kind="real",
            provider="test",
            description="core-loop stability fake",
        )
    )


async def _drive(monkeypatch, script, *, context=None, usage=(10, 5), intent="检索并总结"):
    fake = _ScriptedModel(script, usage=usage)
    proxy = _SettingsProxy(get_settings())
    monkeypatch.setattr(orch, "get_settings", lambda: proxy)
    monkeypatch.setattr(
        orch,
        "_build_planner_models",
        lambda: (fake, fake, [{"slot": "strong", "class": "_ScriptedModel", "model": fake.model}]),
    )
    task = TaskCreate(intent=intent, workflow_type="generic", context=context or {})
    bus = _RecordingBus()
    handle = await run_task(
        task, RequestContext(tenant_id="t-loop", user_id="u-1", role="admin"), bus=bus
    )
    return fake, task, bus, handle


def _tool_messages_seen(fake) -> list[str]:
    seen: list[str] = []
    for call in fake.calls:
        for message in call:
            if isinstance(message, ToolMessage):
                seen.append(str(message.content))
    return seen


def _call(query: str, call_id: str = "c1") -> dict:
    return {
        "content": "调用工具",
        "tool_calls": [{"name": "research.search", "args": {"query": query}, "id": call_id}],
    }


# --------------------------------------------------------------------------- #
# (a) Tool exception → structured observation, loop survives                   #
# --------------------------------------------------------------------------- #
async def test_tool_exception_becomes_structured_observation(monkeypatch, force_memory_backend):
    async def _explode(args, ctx):  # noqa: ANN001
        raise RuntimeError("backend exploded")

    _register("research.search", _explode)
    script = [_call("q"), {"content": "已根据错误信息收尾", "tool_calls": []}]
    fake, task, _bus, _handle = await _drive(monkeypatch, script)

    meta = task.context["llm_runtime"]
    assert meta["terminated_by"] == "model"  # loop survived and converged
    feedback = _tool_messages_seen(fake)
    structured = [text for text in feedback if '"error"' in text and '"kind"' in text]
    assert structured, f"no structured observation in {feedback}"
    obs = json.loads(structured[0])
    assert obs["ok"] is False
    assert obs["tool"] == "research.search"
    assert obs["status"] == "error"
    assert obs["error"]["message"] == "backend exploded"
    assert obs["error"]["retryable"] is False
    assert obs["attempts"] == 1
    # The record itself is untouched by the feedback shaping.
    record = next(r for r in task.context["tool_invocations"] if r["tool"] == "research.search")
    assert record["status"] == "error"
    assert record["error"] == "backend exploded"


# --------------------------------------------------------------------------- #
# (b) Tool timeout → retried per policy, clean finish                          #
# --------------------------------------------------------------------------- #
async def test_tool_timeout_retries_then_loop_answers(monkeypatch, force_memory_backend):
    calls = {"n": 0}

    async def _slow(args, ctx):  # noqa: ANN001
        calls["n"] += 1
        await asyncio.sleep(0.6)
        return {"ok": True, "summary": "too late"}

    _register("research.search", _slow)
    script = [_call("q"), {"content": "工具超时，已收尾", "tool_calls": []}]
    fake, task, _bus, _handle = await _drive(
        monkeypatch,
        script,
        context={**_FAST_RETRY, "tool_step_timeout_seconds": 0.05, "retry_max_attempts": 3},
    )

    assert calls["n"] == 3  # initial + 2 policy retries, each cut at 0.05s
    meta = task.context["llm_runtime"]
    assert meta["terminated_by"] == "model"
    feedback = _tool_messages_seen(fake)
    timeouts = [text for text in feedback if '"timeout": true' in text]
    assert timeouts, f"no timeout observation in {feedback}"
    obs = json.loads(timeouts[0])
    assert obs["error"]["kind"] == "timeout"
    assert obs["error"]["retryable"] is True
    assert obs["attempts"] == 3


# --------------------------------------------------------------------------- #
# (c) Dead loop → terminated_by == "dead_loop"                                 #
# --------------------------------------------------------------------------- #
async def test_dead_loop_stops_with_explicit_reason(monkeypatch, force_memory_backend):
    calls = {"n": 0}

    async def _ok(args, ctx):  # noqa: ANN001
        calls["n"] += 1
        return {"ok": True, "provider": "test", "summary": "same result"}

    _register("research.search", _ok)
    # One scripted reply, repeated forever by the harness: the model
    # keeps asking for the exact same call.
    script = [_call("same")]
    fake, task, bus, _handle = await _drive(monkeypatch, script)

    # The orchestrator may replan after a guard stop (acceptance still
    # unmet) and later attempts overwrite llm_runtime, so the loop's
    # own stop is asserted on the event stream: a run.warning with the
    # dead_loop reason, preceded by exactly two executed rounds of the
    # identical call — the third identical proposal was never executed.
    warn_idx = next(
        i
        for i, (t, d) in enumerate(bus.events)
        if t == "run.warning" and d.get("reason") == "dead_loop"
    )
    warning = bus.events[warn_idx][1]
    assert warning["tool"] == "research.search"
    assert warning["count"] == 3
    steps_before = [
        d
        for t, d in bus.events[:warn_idx]
        if t == "run.step" and d.get("tool") == "research.search"
    ]
    assert len(steps_before) == 2
    assert calls["n"] >= 2


# --------------------------------------------------------------------------- #
# (d) Oversized output → truncated feedback, full record                       #
# --------------------------------------------------------------------------- #
async def test_oversized_output_truncated_for_model_not_record(monkeypatch, force_memory_backend):
    # 3000 chars: under ToolExecutor's own 4000-char payload bound, so
    # the record keeps the payload verbatim; the loop's model-feedback
    # limit (300, set below) is what truncates.
    blob = "x" * 3000

    async def _big(args, ctx):  # noqa: ANN001
        return {"ok": True, "provider": "test", "blob": blob, "summary": "big"}

    _register("research.search", _big)
    script = [_call("q"), {"content": "已收尾", "tool_calls": []}]
    fake, task, _bus, _handle = await _drive(
        monkeypatch, script, context={"tool_feedback_max_chars": 300}
    )

    feedback = _tool_messages_seen(fake)
    truncated = [text for text in feedback if "[输出已截断" in text]
    assert truncated, f"no truncation marker in {feedback}"
    assert len(truncated[0]) < 1000
    assert "原长" in truncated[0]
    meta = task.context["llm_runtime"]
    assert meta.get("truncated_feedbacks", 0) >= 1
    # The stored record keeps the full payload (records store the
    # payload verbatim or as its canonical JSON string — either way
    # the complete blob is in the record, unlike the model feedback).
    record = next(r for r in task.context["tool_invocations"] if r["tool"] == "research.search")
    payload = record["payload"]
    payload_text = payload if isinstance(payload, str) else json.dumps(payload)
    assert blob in payload_text


# --------------------------------------------------------------------------- #
# (e) Token budget cap                                                          #
# --------------------------------------------------------------------------- #
async def test_token_budget_cap_stops_loop(monkeypatch, force_memory_backend):
    async def _ok(args, ctx):  # noqa: ANN001
        return {"ok": True, "provider": "test", "summary": "ok"}

    _register("research.search", _ok)
    script = [
        _call("q1", "c1"),
        _call("q2", "c2"),
        _call("q3", "c3"),
        {"content": "不应到达", "tool_calls": []},
    ]
    # Each model call reports 15 tokens; a budget of 20 is blown by
    # the second call (30 > 20).
    fake, task, _bus, _handle = await _drive(
        monkeypatch, script, context={"loop_token_budget": 20}, usage=(10, 5)
    )
    meta = task.context["llm_runtime"]
    assert meta["terminated_by"] == "token_budget"
    assert meta["loop_guard"]["budget"] == 20
    # Cumulative across the run's usage entries (replan attempts
    # included): the exact total depends on how many attempts spent
    # tokens before the cap fired, but it must exceed the budget.
    assert meta["loop_guard"]["tokens"] > 20
    assert len(fake.calls) <= 4


# --------------------------------------------------------------------------- #
# (f) Consecutive errors → repeated_errors                                     #
# --------------------------------------------------------------------------- #
async def test_consecutive_errors_stop_loop(monkeypatch, force_memory_backend):
    calls = {"n": 0}

    async def _fail(args, ctx):  # noqa: ANN001
        calls["n"] += 1
        raise RuntimeError("kaput")

    _register("research.search", _fail)
    script = [
        _call("q1", "c1"),
        _call("q2", "c2"),
        _call("q3", "c3"),
        {"content": "不应到达", "tool_calls": []},
    ]
    fake, task, bus, _handle = await _drive(
        monkeypatch, script, context={"dead_loop_error_limit": 1}
    )
    # Same event-stream assertion as the dead-loop test: the guard's
    # own run.warning is the durable record of the loop stop (later
    # replan attempts may overwrite llm_runtime).
    warn_idx = next(
        i
        for i, (t, d) in enumerate(bus.events)
        if t == "run.warning" and d.get("reason") == "repeated_errors"
    )
    warning = bus.events[warn_idx][1]
    assert warning["count"] == 2  # limit 1 → the 2nd consecutive error stops it
    assert warning["limit"] == 1
    steps_before = [
        d
        for t, d in bus.events[:warn_idx]
        if t == "run.step" and d.get("tool") == "research.search"
    ]
    assert len(steps_before) == 2
    assert calls["n"] >= 2
