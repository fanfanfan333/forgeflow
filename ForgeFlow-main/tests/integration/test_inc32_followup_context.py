"""INC32 T02 — real Follow-up context injection (AC-39 / AC-40).

Two mechanical contract tests, both driven through the real runtime (not a
hand-built object):

  * **AC-39** — a续聊 run is linked, in the backend, to its parent run and to the
    parent's conversation (``RunRecord.parent_run_id`` / ``session_id`` and the
    persisted ``workspace_runs`` projection). The linkage is read back, never
    assembled by a front-end.
  * **AC-40** — when the injection is on, the new run's react system prompt
    **contains the parent run's delivery fingerprint**. ``_resolve_continued_context``
    is the load-bearing dereference: ``test_ac40_reverse_control_without_deref``
    neutralises it and proves the fingerprint disappears — the positive test is
    therefore provably not decorative.

The react prompt is captured with a scripted chat-model stand-in (the same
technique INC17 uses), so no daemon / provider is touched.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

import forgeflow.runtime.orchestrator as orch
from forgeflow.config import get_settings
from forgeflow.runtime.orchestrator import (
    RequestContext,
    TaskCreate,
    get_run_store,
    reset_run_store,
    run_task,
)
from forgeflow.runtime.tool_registry import (
    load_default_bindings,
    reset_registry,
)
from forgeflow.workspace.store import get_workspace_store, reset_workspace_store

pytestmark = pytest.mark.asyncio

TENANT = "t-inc32-followup"
PARENT_INTENT = "为 Acme 整理一份华东销售线索分析摘要"


# --------------------------------------------------------------------------- #
# Test doubles                                                                 #
# --------------------------------------------------------------------------- #
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
    """Returns scripted ``AIMessage``s; records every message list it saw."""

    def __init__(self, script: list[dict], usage: tuple[int, int] = (10, 5)) -> None:
        self._script = list(script)
        self._usage = usage
        self._last: dict = {"content": "", "tool_calls": []}
        self.calls: list[list] = []
        self.model = "fake-followup-model"
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


def _patch_react(monkeypatch, fake: _ScriptedModel) -> None:
    proxy = _SettingsProxy(get_settings(), mode="react", provider="mock")
    monkeypatch.setattr(orch, "get_settings", lambda: proxy)
    monkeypatch.setattr(
        orch,
        "_build_planner_models",
        lambda: (
            fake,
            fake,
            [{"slot": "strong", "class": "_ScriptedModel", "model": fake.model}],
        ),
    )


@pytest.fixture(autouse=True)
def _clean_state():
    reset_run_store()
    reset_workspace_store()
    load_default_bindings()
    yield
    reset_run_store()
    reset_workspace_store()
    reset_registry()


def _ctx() -> RequestContext:
    return RequestContext(tenant_id=TENANT, user_id="u-inc32", role="admin")


async def _run_parent() -> str:
    """Run a real offline (deterministic) parent task; return its run id."""
    handle = await run_task(
        TaskCreate(intent=PARENT_INTENT, workflow_type="generic"),
        _ctx(),
        bus=_RecordingBus(),
    )
    return handle.run_id


# --------------------------------------------------------------------------- #
# AC-39 — the Follow-up is linked to the parent run + conversation, backend-side #
# --------------------------------------------------------------------------- #
async def test_followup_run_records_parent_and_session(force_memory_backend):
    parent_id = await _run_parent()
    parent = get_run_store().get(parent_id)
    assert parent is not None

    child = await run_task(
        TaskCreate(
            intent="继续：补充客户分层",
            workflow_type="generic",
            context={"continued_from_run_id": parent_id},
        ),
        _ctx(),
        bus=_RecordingBus(),
    )

    child_rec = get_run_store().get(child.run_id)
    assert child_rec is not None
    # Read straight off the runtime record — no front-end concatenation.
    assert child_rec.parent_run_id == parent_id
    assert child_rec.session_id == parent.session_id

    # And the relationship survives in the restart-durable projection.
    stored = await get_workspace_store().get(TENANT, child.run_id)
    assert stored is not None
    assert stored.parent_run_id == parent_id
    assert stored.session_id == parent.session_id


# --------------------------------------------------------------------------- #
# AC-40 — the parent's delivery fingerprint reaches the react prompt            #
# --------------------------------------------------------------------------- #
async def test_ac40_parent_fingerprint_is_injected_into_react_prompt(
    monkeypatch, force_memory_backend
):
    parent_id = await _run_parent()
    parent = get_run_store().get(parent_id)
    assert parent is not None and parent.artifacts
    fingerprint = str(parent.artifacts[0]["result_ref"])
    assert fingerprint, "the parent run must expose a real content fingerprint"

    fake = _ScriptedModel([{"content": "续聊完成", "tool_calls": []}])
    _patch_react(monkeypatch, fake)

    await run_task(
        TaskCreate(
            intent="继续：补充客户分层",
            workflow_type="generic",
            context={"continued_from_run_id": parent_id},
        ),
        _ctx(),
        bus=_RecordingBus(),
    )

    assert fake.calls, "the react executor must have called the model at least once"
    system = fake.calls[0][0]
    assert "上一轮任务" in system.content
    # The parent run's real content fingerprint is carried into the model-visible
    # prompt — the mechanical proof the injection is load-bearing.
    assert fingerprint in system.content


async def test_ac40_reverse_control_without_deref(monkeypatch, force_memory_backend):
    """Negative control: neutralise the dereference ⇒ the fingerprint vanishes.

    Proves the positive AC-40 assertion actually depends on
    ``_resolve_continued_context`` (not on some incidental string).
    """
    parent_id = await _run_parent()
    parent = get_run_store().get(parent_id)
    fingerprint = str(parent.artifacts[0]["result_ref"])

    monkeypatch.setattr(orch, "_resolve_continued_context", lambda context: "")

    fake = _ScriptedModel([{"content": "续聊完成", "tool_calls": []}])
    _patch_react(monkeypatch, fake)

    await run_task(
        TaskCreate(
            intent="继续：补充客户分层",
            workflow_type="generic",
            context={"continued_from_run_id": parent_id},
        ),
        _ctx(),
        bus=_RecordingBus(),
    )

    system = fake.calls[0][0]
    assert "上一轮任务" not in system.content
    assert fingerprint not in system.content
