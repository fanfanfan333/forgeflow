"""FF-R1 B1 (FF-G2 / FF-G3) — provider degradation at the run-record level.

Companion to ``tests/unit/test_ff_r1_b1_degraded_provider.py`` (which pins
the executor internals). This file pins what an API consumer can observe
after ``run_task``:

  1. ``react`` mode + an unreachable provider (every model call dies with
     ``httpx.ConnectTimeout``): the run **fails explicitly** — errors are
     non-empty and carry the ``model_unavailable`` reason code, the
     provider and the model. It is *not* silently completed by the
     deterministic fallback (the pre-R1.1 behaviour).
  2. ``react`` mode + the explicit mock profile: the deterministic route
     is taken *by detection* (marker ``mock_model_no_tool_calling``), the
     run is not presented as a clean model success, and the record is
     honest about it (``degraded_from == "react"``).
  3. Record honesty for the degradations that remain (a generic model
     exception mid-loop): ``record.runtime_mode`` is the mode that
     actually produced the run (``"deterministic"``), ``record.llm``
     carries ``degraded_from == "react"``, and ``record.runtime_mode``
     agrees with the ``run.plan.started`` event of the executor that
     actually ran the plan. For the explicit-failure case (1) no fallback
     ran, so the record keeps ``"react"`` and has no ``degraded_from``.
  4. Recovery consistency: for the terminally failed run of (1), the
     recovery projection must not claim a resumable paused checkpoint —
     ``RecoveryStore.get`` raises ``RecoveryBlocked`` (the API turns that
     into 404) or reports a non-``paused`` status.
"""

from __future__ import annotations

import httpx
import pytest

import forgeflow.config as config_module
import forgeflow.runtime.orchestrator as orch
from forgeflow.config import get_settings
from forgeflow.models.provider import MockChatModel
from forgeflow.runtime.orchestrator import (
    RequestContext,
    TaskCreate,
    get_run_store,
    reset_run_store,
    run_task,
)
from forgeflow.runtime.recovery import RecoveryBlocked, RecoveryStore
from forgeflow.runtime.tool_registry import load_default_bindings, reset_registry

pytestmark = pytest.mark.asyncio

_REQUEST = httpx.Request("POST", "http://localhost:11434/api/chat")
_TENANT = "t-ff-r1-b1-integ"
_USER = "u-1"


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
    import forgeflow.runtime.react_executor as react_module

    monkeypatch.setattr(react_module, "backoff_delay", lambda *a, **k: 0.0)


class _FailingBound:
    def __init__(self, model) -> None:
        self._model = model

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        return self._model._next()


class _UnreachableModel:
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


class _ExplodingModel:
    """A model that dies mid-loop with a non-provider exception."""

    model = "exploding-model"
    _llm_type = "ollama"

    def bind_tools(self, tools):  # noqa: ANN001
        return _FailingBound(self)

    def bind(self, **kwargs):  # noqa: ANN003
        return _FailingBound(self)

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        return self._next()

    def _next(self):
        raise RuntimeError("boom mid-loop")


async def _drive(monkeypatch, model, *, provider="ollama", llm_type="ollama", model_name=None):
    load_default_bindings()
    _patch_settings(monkeypatch, mode="react", provider=provider)
    _patch_models(monkeypatch, model, llm_type=llm_type, model_name=model_name)
    task = TaskCreate(intent="检索并总结", workflow_type="generic", context={})
    bus = _RecordingBus()
    ctx = RequestContext(tenant_id=_TENANT, user_id=_USER, role="admin")
    handle = await run_task(task, ctx, bus=bus)
    record = get_run_store().get(handle.run_id)
    return task, bus, handle, record


async def test_unreachable_provider_run_fails_with_reason_code(monkeypatch) -> None:
    model = _UnreachableModel()
    task, bus, handle, record = await _drive(monkeypatch, model)

    assert record is not None
    assert record.status == "failed"
    assert record.errors, "errors must be non-empty"
    joined = "\n".join(str(e) for e in record.errors)
    assert "model_unavailable" in joined
    assert "ollama" in joined
    assert "qwen-fake-unreachable" in joined

    # No fallback ran, so the record honestly keeps the dispatched mode
    # and carries no degradation provenance.
    assert record.runtime_mode == "react"
    assert "degraded_from" not in (record.llm or {})
    plan_started = bus.of_type("run.plan.started")
    assert plan_started and plan_started[0].get("runtime_mode") == record.runtime_mode


async def test_mock_profile_takes_declared_deterministic_route(monkeypatch) -> None:
    task, bus, handle, record = await _drive(
        monkeypatch, MockChatModel(), provider="mock", llm_type="mock", model_name="mock"
    )

    assert record is not None
    llm = record.llm or {}
    assert llm.get("degraded") == "mock_model_no_tool_calling"
    assert "NotImplementedError" not in str(llm.get("degraded"))
    # The record names the mode that actually produced the run.
    assert record.runtime_mode == "deterministic"
    assert llm.get("degraded_from") == "react"
    # A degraded run is never presented as a clean success.
    assert record.status != "completed" or record.errors


async def test_generic_model_exception_degradation_is_recorded_honestly(monkeypatch) -> None:
    task, bus, handle, record = await _drive(monkeypatch, _ExplodingModel())

    assert record is not None
    llm = record.llm or {}
    assert str(llm.get("degraded") or "").startswith("exception: RuntimeError: boom mid-loop")
    assert record.runtime_mode == "deterministic"
    assert llm.get("degraded_from") == "react"


async def test_recovery_projection_consistent_with_failed_run(monkeypatch) -> None:
    model = _UnreachableModel()
    task, bus, handle, record = await _drive(monkeypatch, model)
    assert record is not None and record.status == "failed"

    try:
        checkpoint = RecoveryStore().get(_TENANT, _USER, handle.run_id)
    except RecoveryBlocked:
        checkpoint = None  # the API projects this as 404 — consistent
    if checkpoint is not None:
        # Anything stored must not present the terminally failed run as a
        # resumable paused checkpoint.
        assert checkpoint["status"] != "paused"
