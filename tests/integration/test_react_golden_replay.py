"""FF-R1 B2 (FF-G11) — golden replay baselines for the ReAct loop.

Four scripted trajectories driven through a real ``run_task`` (scripted
model + fake tools, no paid APIs), pinning the loop's observable shape —
steps, tool_invocations, terminal status and model-call counts — so
future ReAct changes (B3+) must cross this baseline deliberately
instead of drifting silently.

Baselines were recorded against the repo's own harness (the
``test_inc17_react_four_layer`` pattern) on the B2 tree:

  * single tool — 2 model calls, search + report.render, completed.
  * unknown tool — the call never reaches an executor (no invocation
    record for it); the model is told and answers: 2 model calls,
    completed.
  * bad args — after B2's schema validation, the first call returns
    ``TOOL_ARGUMENT_VALIDATION_ERROR`` (handler not run); the error
    ends the attempt, a replan follows, and the model's corrected call
    succeeds: 5 model calls, 4 invocations, completed.
  * duplicate call forever — pre-B3 this burned 13 model calls and
    failed with 「模型未返回最终答复」. Since FF-R1 B3 (FF-G9) the
    third identical proposal forces the final-answer turn; this
    scripted model defies it (it answers every turn, including the
    unbound one, with the same call), so the attempt ends as
    ``dead_loop`` at once and the replan attempts — the streak is
    run-scoped now — end on their first identical proposal:
    6 model calls total (4 + 1 + 1), 2 executions, terminal
    ``failed`` with the dead_loop guard message. (A model that
    honours the forced turn closes in 4 calls, completed — pinned
    in tests/unit/test_identical_call_forced_final.py.)

A fifth test pins the B2 correction cap: the same invalid call may be
corrected at most twice; the third identical call is not executed
again and is answered with a corrections-exhausted validation result.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

import forgeflow.config as config_module
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
    def __init__(self, model: _ScriptedModel) -> None:
        self._model = model

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        return self._model._next(messages)


class _ScriptedModel:
    def __init__(self, script: list[dict]) -> None:
        self._script = list(script)
        self._last: dict = {"content": "", "tool_calls": []}
        self.calls: list[list] = []
        self.model = "fake-golden-model"
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
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )


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


def _tc(name: str, args: dict, call_id: str) -> dict:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


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


async def _drive(monkeypatch, script: list[dict], handler_calls: dict):
    load_default_bindings()

    async def _fake_search(args, ctx):  # noqa: ANN001, ANN202
        handler_calls["n"] += 1
        return {
            "ok": True,
            "provider": "test",
            "query": args.get("query"),
            "results": [],
            "summary": "ok",
        }

    from forgeflow.runtime.tool_registry import resolve

    production_binding = resolve("research.search")
    production = production_binding.args_schema if production_binding else None
    register(
        ToolBinding(
            tool_id="research.search",
            handler=_fake_search,
            kind="real",
            provider="test",
            description="golden fake",
            args_schema=production,
        )
    )
    model = _ScriptedModel(script)
    proxy = _SettingsProxy(get_settings(), mode="react", provider="ollama")
    monkeypatch.setattr(orch, "get_settings", lambda: proxy)
    monkeypatch.setattr(config_module, "get_settings", lambda: proxy)
    info = {"slot": "strong", "class": "_ScriptedModel", "llm_type": "ollama", "model": model.model}
    monkeypatch.setattr(orch, "_build_planner_models", lambda: (model, model, [info]))
    task = TaskCreate(intent="检索并总结", workflow_type="generic", context={})
    bus = _RecordingBus()
    handle = await run_task(
        task, RequestContext(tenant_id="t-golden", user_id="u-1", role="admin"), bus=bus
    )
    invocations = list((task.context or {}).get("tool_invocations") or [])
    return model, handle, invocations


def _shape(invocations) -> list[tuple[str, str, bool]]:
    return [(r.get("tool"), r.get("status"), bool(r.get("executed"))) for r in invocations]


async def test_golden_single_tool(monkeypatch) -> None:
    calls = {"n": 0}
    model, handle, invocations = await _drive(
        monkeypatch,
        [
            {"content": "", "tool_calls": [_tc("research.search", {"query": "forgeflow"}, "c1")]},
            {"content": "最终答案", "tool_calls": []},
        ],
        calls,
    )

    assert handle.status == "completed"
    assert len(model.calls) == 2
    assert _shape(invocations) == [
        ("research.search", "ok", True),
        ("report.render", "ok", True),
    ]
    assert len(handle.detail["steps"]) == 2
    assert calls["n"] == 1


async def test_golden_unknown_tool(monkeypatch) -> None:
    calls = {"n": 0}
    model, handle, invocations = await _drive(
        monkeypatch,
        [
            {"content": "", "tool_calls": [_tc("golden.nonexistent", {}, "c1")]},
            {"content": "最终答案", "tool_calls": []},
        ],
        calls,
    )

    assert handle.status == "completed"
    assert len(model.calls) == 2
    # The unknown call produces no invocation record at all.
    assert _shape(invocations) == [("report.render", "ok", True)]
    assert len(handle.detail["steps"]) == 1
    assert calls["n"] == 0


async def test_golden_bad_args_then_correction(monkeypatch) -> None:
    calls = {"n": 0}
    model, handle, invocations = await _drive(
        monkeypatch,
        [
            {"content": "", "tool_calls": [_tc("research.search", {"query": 123}, "c1")]},
            {"content": "", "tool_calls": [_tc("research.search", {"query": "forgeflow"}, "c2")]},
            {"content": "最终答案", "tool_calls": []},
        ],
        calls,
    )

    # Measured baseline: the validation error ends the first attempt,
    # a replan follows, and the run renders twice (attempt + final) —
    # 5 model calls, 4 invocations, 4 steps.
    assert handle.status == "completed"
    assert len(model.calls) == 5
    assert _shape(invocations) == [
        ("research.search", "error", False),
        ("research.search", "ok", True),
        ("report.render", "ok", True),
        ("report.render", "ok", True),
    ]
    assert len(handle.detail["steps"]) == 4
    validation = invocations[0]
    assert validation["payload"]["error_code"] == "TOOL_ARGUMENT_VALIDATION_ERROR"
    assert calls["n"] == 1, "only the corrected call reached the handler"


async def test_golden_duplicate_call_baseline(monkeypatch) -> None:
    """Post-B3 (FF-G9) baseline — see the module docstring."""
    calls = {"n": 0}
    model, handle, invocations = await _drive(
        monkeypatch,
        [
            {"content": "", "tool_calls": [_tc("research.search", {"query": "forgeflow"}, "c1")]},
        ],
        calls,
    )

    assert handle.status == "failed"
    assert len(model.calls) == 6
    assert _shape(invocations) == [
        ("research.search", "ok", True),
        ("research.search", "ok", True),
        ("report.render", "ok", True),
        ("report.render", "ok", True),
        ("report.render", "ok", True),
    ]
    assert any("dead_loop" in str(e) for e in handle.detail["errors"])


async def test_golden_correction_cap_for_repeated_bad_args(monkeypatch) -> None:
    """The same invalid call gets at most 2 correction chances."""
    calls = {"n": 0}
    model, handle, invocations = await _drive(
        monkeypatch,
        [
            {"content": "", "tool_calls": [_tc("research.search", {"query": 123}, "c1")]},
        ],
        calls,
    )

    search_records = [r for r in invocations if r.get("tool") == "research.search"]
    real_validation = [
        r
        for r in search_records
        if (r.get("payload") or {}).get("error_code") == "TOOL_ARGUMENT_VALIDATION_ERROR"
        and not (r.get("payload") or {}).get("corrections_exhausted")
    ]
    exhausted = [r for r in search_records if (r.get("payload") or {}).get("corrections_exhausted")]
    assert len(real_validation) == 2, "exactly two real validation results"
    assert exhausted, "the third identical call must hit the correction cap"
    assert calls["n"] == 0, "the handler never ran"
    assert handle.status == "failed"
    assert len(model.calls) == 11
