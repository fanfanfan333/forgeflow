"""FF-R1 B1 FIX2 （大川裁定， PR #8 合并前补充）.

Two gaps Codex/大川 found in the B1 round:

  1. The failure surfaces did not always name the **configured** model:
     the probe-degradation message (and its ``run.error`` event) named
     only the provider, and the retry-exhaustion message fell back to
     the built object's class name when the object carried no model
     attribute. Both must carry e.g. ``provider='ollama'
     model='qwen2.5vl:3b'`` — the name from settings.
  2. The startup reachability probe (``_ollama_available``) tried the
     daemon exactly once: a transient blip at startup degraded the
     whole deployment to the mock. The probe now retries per
     ``retry_policy`` (policy ``max_attempts``, short backoff) before
     reporting unavailable; the sleep is injectable so tests stay fast.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

import forgeflow.config as config_module
import forgeflow.models.provider as provider_module
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
from forgeflow.runtime.retry_policy import StepPolicy
from forgeflow.runtime.tool_registry import load_default_bindings, reset_registry

_REQUEST = httpx.Request("POST", "http://localhost:11434/api/chat")
_CONFIGURED_MODEL = "qwen2.5vl:3b"


# --------------------------------------------------------------------------- #
# 2. Probe retries                                                             #
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _reset_probe_cache():
    provider_module._ollama_probe_cache.update({"at": 0.0, "ok": False})
    yield
    provider_module._ollama_probe_cache.update({"at": 0.0, "ok": False})


def _settings() -> SimpleNamespace:
    return SimpleNamespace(ollama_base_url="http://127.0.0.1:9")


def test_probe_retries_until_policy_attempts_exhausted(monkeypatch) -> None:
    calls = {"n": 0}

    def _down(settings) -> bool:  # noqa: ANN001
        calls["n"] += 1
        return False

    monkeypatch.setattr(provider_module, "_ollama_probe_once", _down)
    sleeps: list[float] = []
    policy = StepPolicy(max_attempts=3, initial_backoff_seconds=0.01, jitter_seconds=0.0)

    ok = provider_module._ollama_available(_settings(), policy=policy, sleep=sleeps.append)

    assert ok is False
    assert calls["n"] == 3, "probe attempts must equal the policy's max_attempts"
    assert len(sleeps) == 2, "one short backoff between attempts, none after the last"
    assert all(delay > 0 for delay in sleeps)


def test_probe_attempts_equal_the_configured_value(monkeypatch) -> None:
    calls = {"n": 0}

    def _down(settings) -> bool:  # noqa: ANN001
        calls["n"] += 1
        return False

    monkeypatch.setattr(provider_module, "_ollama_probe_once", _down)
    sleeps: list[float] = []
    policy = StepPolicy(max_attempts=2, initial_backoff_seconds=0.01, jitter_seconds=0.0)

    ok = provider_module._ollama_available(_settings(), policy=policy, sleep=sleeps.append)

    assert ok is False
    assert calls["n"] == 2
    assert len(sleeps) == 1


def test_probe_stops_retrying_once_the_daemon_answers(monkeypatch) -> None:
    outcomes = iter([False, True])
    calls = {"n": 0}

    def _flaky(settings) -> bool:  # noqa: ANN001
        calls["n"] += 1
        return next(outcomes)

    monkeypatch.setattr(provider_module, "_ollama_probe_once", _flaky)
    sleeps: list[float] = []
    policy = StepPolicy(max_attempts=3, initial_backoff_seconds=0.01, jitter_seconds=0.0)

    ok = provider_module._ollama_available(_settings(), policy=policy, sleep=sleeps.append)

    assert ok is True
    assert calls["n"] == 2
    assert len(sleeps) == 1


# --------------------------------------------------------------------------- #
# 1. The configured model name reaches run.error and the failure message       #
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
    def __init__(self, real, *, mode=_UNSET, provider=_UNSET, ollama_model=_UNSET) -> None:
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_mode", mode)
        object.__setattr__(self, "_provider", provider)
        object.__setattr__(self, "_ollama_model", ollama_model)

    def __getattr__(self, item: str):
        if item == "agent_runtime_mode" and self._mode is not _UNSET:
            return self._mode
        if item == "llm_provider" and self._provider is not _UNSET:
            return self._provider
        if item == "ollama_model" and self._ollama_model is not _UNSET:
            return self._ollama_model
        return getattr(self._real, item)


@pytest.fixture(autouse=True)
def _clean_state():
    reset_run_store()
    yield
    reset_run_store()
    reset_registry()


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(react_module, "backoff_delay", lambda *a, **k: 0.0)


async def _drive(monkeypatch, model, *, llm_type: str, model_name):
    load_default_bindings()
    proxy = _SettingsProxy(
        get_settings(), mode="react", provider="ollama", ollama_model=_CONFIGURED_MODEL
    )
    monkeypatch.setattr(orch, "get_settings", lambda: proxy)
    monkeypatch.setattr(config_module, "get_settings", lambda: proxy)
    info = {
        "slot": "strong",
        "class": type(model).__name__,
        "llm_type": llm_type,
        "model": model_name,
    }
    monkeypatch.setattr(orch, "_build_planner_models", lambda: (model, model, [info]))
    task = TaskCreate(intent="检索并总结", workflow_type="generic", context={})
    bus = _RecordingBus()
    handle = await run_task(
        task, RequestContext(tenant_id="t-fix2", user_id="u-1", role="admin"), bus=bus
    )
    return task, bus, handle


async def test_probe_degraded_failure_names_the_configured_model(monkeypatch) -> None:
    """Provider built the mock (probe failed): error must name the model."""
    task, bus, handle = await _drive(
        monkeypatch, MockChatModel(), llm_type="mock", model_name="mock"
    )

    assert handle.status == "failed"
    joined = "\n".join(str(e) for e in handle.detail["errors"])
    assert "ollama" in joined
    assert _CONFIGURED_MODEL in joined, f"configured model missing from errors: {joined}"

    run_errors = [d for d in bus.of_type("run.error") if d.get("code") == "model_unavailable"]
    assert run_errors, "a model_unavailable run.error event is required"
    assert run_errors[0].get("model") == _CONFIGURED_MODEL


class _FailingBound:
    def __init__(self, model) -> None:  # noqa: ANN001
        self._model = model

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        return self._model._next()


class _UnnamedUnreachableModel:
    """Every call dies on the provider; the object itself names no model."""

    model = None
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


async def test_exhausted_failure_names_the_configured_model(monkeypatch) -> None:
    """Retry exhaustion: the configured name wins over the class name."""
    task, bus, handle = await _drive(
        monkeypatch, _UnnamedUnreachableModel(), llm_type="ollama", model_name=None
    )

    assert handle.status == "failed"
    joined = "\n".join(str(e) for e in handle.detail["errors"])
    assert _CONFIGURED_MODEL in joined, f"configured model missing from errors: {joined}"
    assert "model_unavailable" in joined

    run_errors = [d for d in bus.of_type("run.error") if d.get("code") == "model_unavailable"]
    assert run_errors
    assert run_errors[0].get("model") == _CONFIGURED_MODEL
