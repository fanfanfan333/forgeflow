"""FF-R1 B1 (R1.1) — provider unavailability must fail loudly, never degrade silently.

Root cause pinned by these tests (verified against main 446a850):

  * ``httpx.ConnectTimeout`` has an empty ``str()`` and its class name is not in
    ``retry_policy._RETRYABLE_TYPE_NAMES``, so ``classify_error`` judged it
    non-retryable; the ReAct loop re-raised on the first attempt and the
    catch-all in ``_run_loop`` silently fell back to the deterministic
    executor, leaving only ``"ConnectTimeout: "`` in the degraded marker.
  * A real provider that builds the mock model (reachability probe failed)
    only produced a warning and then ran against the deterministic stub.
  * ``MockChatModel.bind_tools`` raises ``NotImplementedError``; that raw
    exception was the (empty) degradation reason for the offline profile.

Expected behaviour after the fix:

  * httpx timeout / network errors are retryable **by type**;
  * error messages use ``str(exc) or repr(exc)`` and carry provider + model;
  * an unavailable provider retries (policy attempts), then the run fails
    explicitly — status ``failed``, reason ``failed_retryable``, code
    ``model_unavailable``, exposed via ``run.error`` — instead of degrading;
  * mock / non-tool-calling models are detected when entering ReAct and get
    a clear error, never a raw ``NotImplementedError``; an explicitly
    configured mock/deterministic profile still runs as before.
"""

from __future__ import annotations

import httpx
import pytest
from langchain_core.messages import AIMessage

import forgeflow.config as config_module
import forgeflow.runtime.orchestrator as orch
import forgeflow.runtime.react_executor as react_module
from forgeflow.config import get_settings
from forgeflow.models.provider import MockChatModel
from forgeflow.runtime.orchestrator import (
    RequestContext,
    TaskCreate,
    reset_run_store,
    run_task,
)
from forgeflow.runtime.retry_policy import classify_error
from forgeflow.runtime.tool_registry import load_default_bindings, reset_registry

_REQUEST = httpx.Request("POST", "http://localhost:11434/api/chat")


# --------------------------------------------------------------------------- #
# classify_error: httpx transport errors are retryable by type                 #
# --------------------------------------------------------------------------- #
def test_classify_httpx_timeouts_are_retryable() -> None:
    for exc in (
        httpx.ConnectTimeout("", request=_REQUEST),
        httpx.ReadTimeout("", request=_REQUEST),
        httpx.WriteTimeout("", request=_REQUEST),
        httpx.PoolTimeout("", request=_REQUEST),
    ):
        kind, retryable = classify_error(exc)
        assert retryable is True, f"{type(exc).__name__} must be retryable"
        assert kind == "timeout"


def test_classify_httpx_network_errors_are_retryable() -> None:
    for exc in (
        httpx.ConnectError("", request=_REQUEST),
        httpx.ReadError("", request=_REQUEST),
        httpx.WriteError("", request=_REQUEST),
    ):
        kind, retryable = classify_error(exc)
        assert retryable is True, f"{type(exc).__name__} must be retryable"
        assert kind == "connection"


def test_classify_httpx_subclass_with_unlisted_name_is_retryable() -> None:
    """Type-based, not name-based: an unknown subclass name still retries."""

    class _AlienConnectTimeout(httpx.ConnectTimeout):
        pass

    _kind, retryable = classify_error(_AlienConnectTimeout("", request=_REQUEST))
    assert retryable is True


def test_classify_unrelated_errors_stay_non_retryable() -> None:
    assert classify_error(ValueError("bad input"))[1] is False
    assert classify_error(RuntimeError("boom"))[1] is False


# --------------------------------------------------------------------------- #
# Run-level harness (mirrors tests/unit/test_inc17_react_loop.py)              #
# --------------------------------------------------------------------------- #
class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, run_id: str, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))

    def of_type(self, event_type: str) -> list[dict]:
        return [data for kind, data in self.events if kind == event_type]


_UNSET = object()


class _SettingsProxy:
    def __init__(self, real, *, mode=_UNSET, provider=_UNSET) -> None:
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_mode", mode)
        object.__setattr__(self, "_provider", provider)

    def __getattr__(self, item: str):
        if item == "agent_runtime_mode" and self._mode is not _UNSET:
            return self._mode
        if item == "llm_provider" and self._provider is not _UNSET:
            return self._provider
        return getattr(self._real, item)


def _patch_settings(monkeypatch, *, mode="react", provider="ollama") -> None:
    proxy = _SettingsProxy(get_settings(), mode=mode, provider=provider)
    monkeypatch.setattr(orch, "get_settings", lambda: proxy)
    monkeypatch.setattr(config_module, "get_settings", lambda: proxy)


def _patch_models(monkeypatch, model, *, llm_type="ollama", model_name=None) -> None:
    info = {
        "slot": "strong",
        "class": type(model).__name__,
        "llm_type": llm_type,
        "model": model_name if model_name is not None else getattr(model, "model", None),
    }
    monkeypatch.setattr(orch, "_build_planner_models", lambda: (model, model, [info]))


@pytest.fixture(autouse=True)
def _clean_state():
    reset_run_store()
    yield
    reset_run_store()
    reset_registry()


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    """Retries must still happen — only the sleep between them is removed."""
    monkeypatch.setattr(react_module, "backoff_delay", lambda *a, **k: 0.0)


def _ctx() -> RequestContext:
    return RequestContext(tenant_id="t-ff-r1-b1", user_id="u-1", role="admin")


async def _drive(monkeypatch, model, *, provider="ollama", llm_type="ollama", model_name=None):
    load_default_bindings()
    _patch_settings(monkeypatch, mode="react", provider=provider)
    _patch_models(monkeypatch, model, llm_type=llm_type, model_name=model_name)
    task = TaskCreate(intent="检索并总结", workflow_type="generic", context={})
    bus = _RecordingBus()
    handle = await run_task(task, _ctx(), bus=bus)
    return task, bus, handle


class _FailingBound:
    def __init__(self, model: _UnreachableModel) -> None:
        self._model = model

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        return self._model._next()


class _UnreachableModel:
    """A chat model whose every call dies on an unreachable provider."""

    model = "qwen-fake-unreachable"
    _llm_type = "ollama"

    def __init__(self) -> None:
        self.calls = 0

    def bind_tools(self, tools):  # noqa: ANN001
        return _FailingBound(self)

    def bind(self, **kwargs):  # noqa: ANN003
        return _FailingBound(self)

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        return self._next()

    def _next(self):
        self.calls += 1
        raise httpx.ConnectTimeout("", request=_REQUEST)


class _NoToolCallingModel:
    """A real-provider model that cannot bind tools (bind_tools raises)."""

    model = "ni-model-1"
    _llm_type = "ollama"

    def bind_tools(self, tools):  # noqa: ANN001
        raise NotImplementedError

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        return AIMessage(content="unreachable")


class _ScriptedBound:
    def __init__(self, model: _ScriptedModel) -> None:
        self._model = model

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        return self._model._next()


class _ScriptedModel:
    model = "fake-scripted"
    _llm_type = "fake"

    def __init__(self, script: list[dict]) -> None:
        self._script = list(script)
        self._last: dict = {"content": "", "tool_calls": []}

    def bind_tools(self, tools):  # noqa: ANN001
        return _ScriptedBound(self)

    def bind(self, **kwargs):  # noqa: ANN003
        return _ScriptedBound(self)

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        return self._next()

    def _next(self) -> AIMessage:
        if self._script:
            self._last = self._script.pop(0)
        return AIMessage(
            content=self._last.get("content", ""),
            tool_calls=self._last.get("tool_calls", []),
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )


# --------------------------------------------------------------------------- #
# Run-level expectations                                                       #
# --------------------------------------------------------------------------- #
async def test_unreachable_provider_fails_explicitly_after_retries(monkeypatch) -> None:
    model = _UnreachableModel()
    task, bus, handle = await _drive(monkeypatch, model)

    # Retried before failing (policy: 1 initial + 2 retries on the turn call,
    # plus the planning call) — a single attempt would prove no retry happened.
    assert model.calls >= 3

    assert handle.status == "failed"
    errors = handle.detail["errors"]
    assert errors, "errors must be non-empty when the provider is unavailable"
    joined = "\n".join(str(e) for e in errors)
    assert "ollama" in joined  # provider named
    assert "qwen-fake-unreachable" in joined  # model named
    assert "ConnectTimeout" in joined  # repr fallback: str(exc) was empty
    assert "model_unavailable" in joined  # reason code surfaced in the error

    meta = task.context["llm_runtime"]
    assert meta.get("failure_code") == "model_unavailable"
    assert meta.get("failure_reason") == "failed_retryable"
    assert not meta.get("degraded"), "no silent deterministic degradation"

    run_errors = bus.of_type("run.error")
    assert any(
        data.get("code") == "model_unavailable" and data.get("retryable") is True
        for data in run_errors
    ), f"run.error must expose the retryable failure, got {run_errors}"

    records = task.context.get("tool_invocations") or []
    assert all(not r.get("executed") for r in records), "no tool may execute"


async def test_mock_model_under_real_provider_fails_explicitly(monkeypatch) -> None:
    task, bus, handle = await _drive(
        monkeypatch, MockChatModel(), provider="ollama", llm_type="mock", model_name="mock"
    )

    assert handle.status == "failed"
    errors = handle.detail["errors"]
    assert errors
    joined = "\n".join(str(e) for e in errors)
    assert "ollama" in joined
    assert "mock" in joined
    assert "model_unavailable" in joined

    meta = task.context["llm_runtime"]
    assert meta.get("failure_code") == "model_unavailable"
    # An explicit failure is not a degradation: the ``degraded`` marker
    # would make the verdict funnel claim the deterministic fallback
    # completed this run (and rewrite the record's runtime_mode).
    assert not meta.get("degraded")
    assert any(data.get("code") == "model_unavailable" for data in bus.of_type("run.error"))


async def test_model_without_tool_calling_fails_with_clear_error(monkeypatch) -> None:
    task, bus, handle = await _drive(monkeypatch, _NoToolCallingModel())

    assert handle.status == "failed"
    errors = handle.detail["errors"]
    assert errors
    joined = "\n".join(str(e) for e in errors)
    assert "ni-model-1" in joined
    assert "不支持工具调用" in joined
    assert "NotImplementedError" not in joined

    meta = task.context["llm_runtime"]
    assert meta.get("failure_code") == "model_tool_calling_unsupported"
    assert not meta.get("degraded")


async def test_explicit_mock_profile_with_mock_model_still_runs(monkeypatch) -> None:
    """The offline mock profile is explicit configuration, not a failure.

    Counterfactual on unmodified main: this same run ends as an explicit
    degradation (status ``failed`` — a degraded run is never a clean
    success) whose recorded reason is the raw, message-less
    ``exception: NotImplementedError: ``. After the fix the outcome shape
    is identical (deterministic route, degraded flag set) but the reason
    is the diagnosable marker, reached by detection — not by swallowing
    a ``NotImplementedError``.
    """
    task, bus, handle = await _drive(
        monkeypatch, MockChatModel(), provider="mock", llm_type="mock", model_name="mock"
    )

    assert handle.status == "failed"
    joined = "\n".join(str(e) for e in handle.detail["errors"])
    assert "mock_model_no_tool_calling" in joined
    assert "NotImplementedError" not in joined
    meta = task.context["llm_runtime"]
    assert meta.get("degraded") == "mock_model_no_tool_calling"


async def test_explicit_mock_profile_with_scripted_model_still_runs(monkeypatch) -> None:
    model = _ScriptedModel([{"content": "已完成"}])
    task, bus, handle = await _drive(
        monkeypatch, model, provider="mock", llm_type="fake", model_name="fake-scripted"
    )

    assert handle.status == "completed"
    assert not handle.detail["errors"]
