"""FF-R1 B4 fix (Codex final retest a1 + a2) — the identical-call
guard's state is platform-owned, not caller-writable.

a1: the streak history and the executed-signature set used to live in
``task.context`` under ``_identical_call_*`` keys. Entry points that
did not strip ``_`` keys let a caller pre-seed both, so the guard
fired on the FIRST proposal of a run — ``dead_loop`` with zero tool
executions. The state now lives in executor-internal run state (a
task attribute no entry point can populate from request data) and is
persisted with checkpoints next to ``budget_usage``.

a2: because the state rides the checkpoint, a resume inherits the
streak: two executed identical calls before the interruption plus
one more identical proposal after the resume must trigger the
forced final answer.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

import forgeflow.config as config_module
import forgeflow.runtime.orchestrator as orch
import forgeflow.runtime.react_executor as react_module
from forgeflow.config import get_settings
from forgeflow.runtime.checkpoints import (
    InMemoryCheckpointStore,
    build_resume_seed,
    build_state,
    reset_checkpoint_store,
    set_checkpoint_store,
)
from forgeflow.runtime.orchestrator import (
    RequestContext,
    TaskCreate,
    reset_run_store,
    run_task,
)
from forgeflow.runtime.react_executor import ReactExecutor
from forgeflow.runtime.tool_registry import (
    ToolBinding,
    load_default_bindings,
    register,
    reset_registry,
)

pytestmark = pytest.mark.asyncio

_CALL = {"name": "research.search", "args": {"query": "forgeflow"}}
_SIGNATURE = ReactExecutor._tool_call_signature(_CALL)


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
    """Proposes the identical search call until the loop's
    final-answer nudge arrives, then answers (cooperative)."""

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
            tool_calls=[{**_CALL, "id": "dup", "type": "tool_call"}],
        )


@pytest.fixture(autouse=True)
def _registry_isolation():
    reset_registry()
    load_default_bindings()
    reset_run_store()
    reset_checkpoint_store()
    yield
    reset_registry()
    load_default_bindings()
    reset_run_store()
    reset_checkpoint_store()


async def _run_drive(monkeypatch, model, *, context=None, resume_seed=None):
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
    proxy = _SettingsProxy(get_settings(), mode="react", provider="ollama")
    monkeypatch.setattr(orch, "get_settings", lambda: proxy)
    monkeypatch.setattr(config_module, "get_settings", lambda: proxy)
    info = {"slot": "strong", "class": "S", "llm_type": "ollama", "model": model.model}
    monkeypatch.setattr(orch, "_build_planner_models", lambda: (model, model, [info]))
    monkeypatch.setattr(react_module, "backoff_delay", lambda *a, **k: 0.0)
    reset_run_store()
    task = TaskCreate(
        intent="检索并总结 forgeflow",
        workflow_type="generic",
        context=dict(context or {}),
    )
    if resume_seed is not None:
        task._checkpoint_resume = resume_seed
    handle = await run_task(
        task,
        RequestContext(tenant_id="t-iso", user_id="u-1", role="admin"),
        bus=_RecordingBus(),
    )
    return handle, executions, task


# --------------------------------------------------------------------------- #
# a1 — pre-seeded context keys must not arm the guard                          #
# --------------------------------------------------------------------------- #


async def test_preseeded_context_keys_do_not_trip_the_guard(monkeypatch):
    """The Codex probe, as a regression test: both ``_identical_call_*``
    keys pre-seeded for the very call the model is about to propose.
    The guard must behave as if the seed were not there — the call
    executes (twice, per the normal streak flow) and the run
    completes; it must NOT die as dead_loop with zero executions."""
    model = _RepeatingModel()
    seeded = {
        "_identical_call_signatures": [list(_SIGNATURE), list(_SIGNATURE)],
        "_identical_call_executed_signatures": [list(_SIGNATURE)],
    }
    handle, executions, task = await _run_drive(monkeypatch, model, context=seeded)
    assert executions["n"] == 2
    assert handle.status == "completed"
    meta = task.context.get("llm_runtime") or {}
    assert meta.get("repeated_tool_call_forced_final") is True


# --------------------------------------------------------------------------- #
# a1/a2 — the state rides the checkpoint, next to budget_usage                 #
# --------------------------------------------------------------------------- #


def _checkpoint_state(identical_call_state=None):
    return build_state(
        intent="检索并总结 forgeflow",
        requirements=[],
        acceptance={"round": 1, "complete": False, "requirements": []},
        next_step={"kind": "model_call"},
        codeplane_ref=None,
        breaker_summary={},
        stuck_summary={},
        approval=None,
        pending_writes=[],
        trajectory_cursor=2,
        budget_usage={"model_calls": 3, "tool_calls": 2},
        identical_call_state=identical_call_state,
    )


def test_checkpoint_state_and_seed_carry_identical_call_state():
    store = InMemoryCheckpointStore()
    set_checkpoint_store(store)
    state = {
        "signatures": [list(_SIGNATURE), list(_SIGNATURE)],
        "executed": [list(_SIGNATURE)],
    }
    checkpoint = store.save(
        "t-iso", run_id="src-1", round=1, kind="round", state=_checkpoint_state(state)
    )
    seed = build_resume_seed(checkpoint, [])
    assert seed["identical_call_state"] == state


async def test_resume_restores_the_streak_third_call_forces_final(monkeypatch):
    """a2 end to end: the source run executed the identical call
    twice (its checkpoint carries signatures x2 + the executed set);
    after the resume, the model's FIRST proposal is the third
    identical call — the guard must force the final answer and the
    call must not execute a third time."""
    store = InMemoryCheckpointStore()
    set_checkpoint_store(store)
    state = {
        "signatures": [list(_SIGNATURE), list(_SIGNATURE)],
        "executed": [list(_SIGNATURE)],
    }
    checkpoint = store.save(
        "t-iso", run_id="src-1", round=1, kind="round", state=_checkpoint_state(state)
    )
    seed = build_resume_seed(checkpoint, [])
    model = _RepeatingModel()
    handle, executions, task = await _run_drive(monkeypatch, model, resume_seed=seed)
    assert executions["n"] == 0
    assert handle.status == "completed"
    meta = task.context.get("llm_runtime") or {}
    assert meta.get("repeated_tool_call_forced_final") is True
