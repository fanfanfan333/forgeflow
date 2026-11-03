"""React-loop wiring proofs, driven through ``run_task`` with a scripted model.

The c01ddb7 batch stated this limit plainly: its read-only refusal and
per-round acceptance history had NO unit wiring test (no react model
harness existed in the unit tests — the llm-path harness drives the llm
path, not the react loop), so the wiring was gate-verified only and had
to be observed in a real round. This file closes that gap: the react
loop is driven offline with a fake model that emits ``AIMessage``
tool calls, so the wiring is proven here, not left to the next real run.

* Read-only refusal: a read-only intent's edit call is refused at the
  (2c) gate — a ``read_only_edit_refused`` warning, no executed edit
  record, and the refusal is attributable via ``llm.read_only``.
* Per-round acceptance history: every tool round appends a snapshot
  (round / attempt / complete / requirements) into ``llm``.
* StuckDetector force-final: an A/B ping-pong over blocked read calls
  (two calls per turn, so the warn → arm → force-final sequence fits
  inside the hard 6-turn ceiling) warns once, arms on a later turn, and
  cuts the loop with ``stuck_forced_final`` instead of hitting the cap.
  Blocked reads are used deliberately: the tool circuit breaker never
  trips on them (consecutive failures per tool stay at 1), which is
  exactly the non-breaker pattern real rounds had never triggered.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

import forgeflow.config as config_mod
import forgeflow.runtime.orchestrator as orch
from forgeflow.config import get_settings
from forgeflow.runtime import llm_planner
from forgeflow.runtime.orchestrator import (
    RequestContext,
    TaskCreate,
    reset_run_store,
    run_task,
)

pytestmark = pytest.mark.asyncio


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, run_id: str, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


class _FakeReactModel:
    """Scripted chat model: returns the scripted AIMessages in order.

    Deliberately implements NO ``bind_tools`` — the react loop's
    ``_bind`` falls back to the model itself — and no provider SDK.
    When the script is exhausted it answers with plain text, so the
    loop terminates instead of hanging.
    """

    def __init__(self, script: list[AIMessage]) -> None:
        self._script = list(script)
        self.calls = 0
        self.model = "fake-react-model"
        self._llm_type = "fake"

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        if self._script:
            return self._script.pop(0)
        return AIMessage(content="已有证据收尾。")


_UNSET = object()


class _SettingsProxy:
    """Read-through proxy overriding mode / provider / tool loading."""

    def __init__(self, real, **overrides) -> None:
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_overrides", overrides)

    def __getattr__(self, item: str):
        if item in self._overrides:
            return self._overrides[item]
        return getattr(self._real, item)


def _patch_react(monkeypatch, fake: _FakeReactModel) -> None:
    proxy = _SettingsProxy(
        get_settings(),
        agent_runtime_mode="react",
        llm_provider="mock",
        agent_tool_loading="direct",
    )
    monkeypatch.setattr(orch, "get_settings", lambda: proxy)
    monkeypatch.setattr(config_mod, "get_settings", lambda: proxy)
    monkeypatch.setattr(
        orch,
        "_build_planner_models",
        lambda: (fake, fake, [{"slot": "strong", "class": "_FakeReactModel", "model": fake.model}]),
    )


@pytest.fixture(autouse=True)
def _clean_state():
    reset_run_store()
    llm_planner.clear_caches()
    yield
    reset_run_store()
    llm_planner.clear_caches()


def _ctx() -> RequestContext:
    return RequestContext(tenant_id="t-react", user_id="u-1", role="admin")


def _call(name: str, args: dict, call_id: str) -> dict:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


async def test_read_only_edit_refused_and_history_recorded(monkeypatch):
    fake = _FakeReactModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _call(
                        "textfile.edit",
                        {"paths": ["/tmp/x.txt"], "edits": [{"old": "a", "new": "b"}]},
                        "call_1",
                    )
                ],
            ),
            AIMessage(content="已只读查看，未修改。"),
        ]
    )
    _patch_react(monkeypatch, fake)
    bus = _RecordingBus()

    handle = await run_task(
        TaskCreate(intent="请只读这份报告，不要修改", workflow_type="generic"),
        _ctx(),
        bus=bus,
    )

    llm = handle.detail["llm"]
    assert llm["read_only"] is True
    # The refusal is attributable on the bus, not silent.
    assert any(
        event == "run.warning" and data.get("reason") == "read_only_edit_refused"
        for event, data in bus.events
    )
    # No executed edit record: the gate refuses before any record exists.
    steps = handle.detail["steps"]
    assert not [
        s for s in steps if s.get("tool") == "textfile.edit" and s.get("executed") is True
    ]
    # Per-round acceptance snapshots exist even for a refusal-only round.
    history = llm["acceptance_history"]
    assert len(history) >= 1
    assert history[0]["round"] >= 1
    assert "complete" in history[0] and "requirements" in history[0]


async def test_stuck_ping_pong_warns_arms_and_forces_final(monkeypatch):
    # Two blocked read calls per turn: inspect + document.inspect on
    # nonexistent paths (varying args, so neither the identical-call
    # guard nor same-error fires; the breaker never trips either).
    script = []
    for turn in (1, 2, 3, 4):
        script.append(
            AIMessage(
                content="",
                tool_calls=[
                    _call("textfile.inspect", {"paths": [f"/nope{turn}.txt"]}, f"call_t{turn}a"),
                    _call(
                        "document.inspect", {"paths": [f"/nope{turn}.docx"]}, f"call_t{turn}b"
                    ),
                ],
            )
        )
    # Turn 5's call is dropped by the force-final guard; its text
    # becomes the final answer.
    script.append(
        AIMessage(
            content="已有证据收尾，不再重复调用。",
            tool_calls=[_call("textfile.inspect", {"paths": ["/nope5.txt"]}, "call_t5a")],
        )
    )
    fake = _FakeReactModel(script)
    _patch_react(monkeypatch, fake)
    bus = _RecordingBus()

    handle = await run_task(
        TaskCreate(intent="整理项目文档", workflow_type="generic"),
        _ctx(),
        bus=bus,
    )

    llm = handle.detail["llm"]
    detected = llm.get("stuck_patterns_detected") or []
    assert any(d.get("key", "").startswith("ping_pong:") for d in detected)
    assert llm.get("stuck_forced_final") is True
    # Cut short by the guard, not by the ceiling (the ceiling invariant
    # forbids disguising a truncated run as an answer).
    assert llm.get("terminated_by") != "max_iterations"
    assert llm.get("iterations", 0) <= 6
    # The dropped turn-5 call never executed: at most the 8 armed calls.
    executed = [s for s in handle.detail["steps"] if s.get("executed") is True]
    assert len(executed) <= 8
