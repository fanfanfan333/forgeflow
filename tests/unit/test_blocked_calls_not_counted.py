"""FF-R1 B4 sync (Codex B3-retest gap) — calls the platform
intercepted must not count toward the identical-call streak.

A proposal stopped by the HITL pre-approval gate or by RBAC (the
allow-list skip / policy denial) never executes. Before this fix the
FF-G9 guard counted those proposals anyway: a model whose call was
waiting on a human, or forbidden for its role, was first forced
into a final-answer turn and then terminated as ``dead_loop`` —
with zero executions. The guard now requires at least one earlier
identical call to have actually executed before it may fire.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

import forgeflow.config as config_module
import forgeflow.runtime.orchestrator as orch
import forgeflow.runtime.react_executor as react_module
from forgeflow.config import get_settings
from forgeflow.runtime.approval_policy import (
    register_pre_approval_tool,
    unregister_pre_approval_tool,
)
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


class _AlwaysCallModel:
    """Proposes one fixed tool call forever (scripted)."""

    def __init__(self, tool: str, args: dict) -> None:
        self.model = "scripted-model"
        self._llm_type = "ollama"
        self.calls = 0
        self._tool = tool
        self._args = args

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        return self

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": self._tool,
                    "args": self._args,
                    "id": "blocked-dup",
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


async def _run_drive(monkeypatch, model, *, role: str = "admin"):
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
    task = TaskCreate(intent="检索并总结 forgeflow", workflow_type="generic", context={})
    handle = await run_task(
        task,
        RequestContext(tenant_id="t-b4s", user_id="u-1", role=role),
        bus=_RecordingBus(),
    )
    return handle, executions, task


def _assert_guard_never_fired(handle, task) -> None:
    meta = task.context.get("llm_runtime") or {}
    assert meta.get("repeated_tool_call_forced_final") is not True
    assert (meta.get("loop_guard") or {}).get("reason") != "dead_loop"
    assert not any(
        "dead_loop" in str(e) or "重复调用已停止" in str(e)
        for e in (handle.detail.get("errors") or [])
    )


async def test_hitl_blocked_repetitions_do_not_count_toward_the_streak(monkeypatch):
    """research.search is pre-approval gated: every proposal waits
    on a human and the handler never runs. The repetitions are the
    platform's block repeating, not a model loop — the guard must
    stay out and the run must surface as awaiting_approval."""
    from forgeflow.hitl import pending as pending_module

    # The pending-action store must be the in-memory one (the pattern
    # of test_inc46_hitl.py): with a Postgres DSN configured but no
    # server reachable, the gate would fail closed to "refused"
    # instead of producing the awaiting_approval state under test.
    pending_module.set_pending_store(pending_module.InMemoryPendingActionStore())
    register_pre_approval_tool("research.search")
    try:
        model = _AlwaysCallModel("research.search", {"query": "forgeflow"})
        handle, executions, task = await _run_drive(monkeypatch, model)
    finally:
        unregister_pre_approval_tool("research.search")
        pending_module.reset_pending_store()

    assert executions["n"] == 0, "the gated handler must never run"
    assert model.calls >= 3, "the drive must actually repeat the blocked call"
    _assert_guard_never_fired(handle, task)
    assert handle.status == "awaiting_approval"


async def test_rbac_blocked_repetitions_do_not_count_toward_the_streak(monkeypatch):
    """A viewer proposing code.execute is stopped by RBAC on every
    turn (allow-list skip; the handler never runs). The guard must
    not convert those denials into a dead_loop termination."""
    model = _AlwaysCallModel("code.execute", {"code": "print(1)"})
    handle, executions, task = await _run_drive(monkeypatch, model, role="viewer")

    invocations = task.context.get("tool_invocations") or []
    assert not any(
        inv.get("tool") == "code.execute" and inv.get("executed") for inv in invocations
    ), "the denied handler must never run"
    assert model.calls >= 3, "the drive must actually repeat the blocked call"
    _assert_guard_never_fired(handle, task)
