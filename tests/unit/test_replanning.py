"""Stall-triggered replanning (8-feature program, Phase 3).

Pure-function proofs for :mod:`forgeflow.runtime.replanning` plus
react-loop wiring proofs driven through ``run_task`` with a scripted
model (mirroring ``test_react_wiring.py``'s harness).

Pinned rules:
* Two consecutive no-progress tool rounds trigger one planning round;
  planning rounds do NOT count toward the 6 execution rounds.
* Planning is capped at 2 per run; the ceiling still ends the loop.
* A planning failure keeps the old plan and never fails the run.
* An unchanged plan is a no-progress signal (no stall reset).
* A model halt does not skip a due planning round.
* Delegated children never plan; planning steps are not tool records
  (StuckDetector never sees them).
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
from forgeflow.runtime.replanning import (
    PLANNING_MARKER,
    count_satisfied,
    parse_plan,
    plans_equal,
    round_progressed,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------- pure


def test_parse_plan_numbering_bullets_and_blocked():
    steps, blocked = parse_plan(
        "1. 先查 A\n- 再查 B\n受阻点：缺 C\n\n3. 最后汇总\n"
    )
    assert steps == ["先查 A", "再查 B", "最后汇总"]
    assert blocked == "缺 C"


def test_parse_plan_empty():
    assert parse_plan("") == ([], "")
    assert parse_plan("   \n  \n") == ([], "")


def test_plans_equal_normalizes_whitespace():
    assert plans_equal([" A ", "B"], ["A", "B"])
    assert not plans_equal(["A"], ["A", "B"])
    assert not plans_equal(["A"], ["B"])


def test_round_progressed_signals():
    rows_satisfied = [{"status": "satisfied"}]
    rows_open = [{"status": "unsatisfied"}]
    assert round_progressed(0, rows_satisfied, []) is True
    assert round_progressed(0, rows_open, [{"tool": "research.verify", "status": "ok"}]) is True
    # Rendering alone is not task advance.
    assert round_progressed(0, rows_open, [{"tool": "report.render", "status": "ok"}]) is False
    # Blocked / failed rounds are stall rounds.
    assert round_progressed(0, rows_open, [{"tool": "textfile.inspect", "status": "blocked"}]) is False
    assert round_progressed(0, rows_open, [{"tool": "textfile.inspect", "status": "error"}]) is False
    assert count_satisfied(rows_satisfied) == 1


# ---------------------------------------------------------------- harness


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, run_id: str, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


class _FakeReactModel:
    """Scripted chat model; records every call's input messages."""

    def __init__(self, script: list[AIMessage]) -> None:
        self._script = list(script)
        self.calls = 0
        self.seen_messages: list[list] = []
        self.model = "fake-react-model"
        self._llm_type = "fake"

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        self.seen_messages.append(list(messages))
        if self._script:
            return self._script.pop(0)
        return AIMessage(content="已有证据收尾。")


class _RaisingPlanningFakeModel(_FakeReactModel):
    """Fails planning calls only (marker in the prompt), never exec calls."""

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        for message in messages:
            if PLANNING_MARKER in str(getattr(message, "content", "")):
                raise RuntimeError("planning model unavailable")
        return await super().ainvoke(messages, **kwargs)


class _SettingsProxy:
    def __init__(self, real, **overrides) -> None:
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_overrides", overrides)

    def __getattr__(self, item: str):
        if item in self._overrides:
            return self._overrides[item]
        return getattr(self._real, item)


def _patch_react(monkeypatch, fake) -> None:
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


def _inspect(path: str, call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[_call("textfile.inspect", {"paths": [path]}, call_id)],
    )


_PLAN_A = AIMessage(content="1. 先查 A\n2. 再查 B\n受阻点：缺 C")


async def _run(monkeypatch, fake, intent="整理项目文档", context=None):
    _patch_react(monkeypatch, fake)
    task = TaskCreate(intent=intent, workflow_type="generic")
    if context:
        task.context.update(context)
    return await run_task(task, _ctx(), bus=_RecordingBus())


async def test_stall_triggers_planning_and_plan_not_counted(monkeypatch):
    fake = _FakeReactModel(
        [
            _inspect("/nope1.txt", "call_1"),
            _inspect("/nope2.txt", "call_2"),
            _PLAN_A,
            AIMessage(content="已有证据收尾。"),
        ]
    )
    handle = await _run(monkeypatch, fake)
    llm = handle.detail["llm"]
    steps = llm.get("planning_steps") or []
    assert len(steps) == 1
    assert steps[0]["status"] == "ok"
    assert steps[0]["plan"] == ["先查 A", "再查 B"]
    assert steps[0]["trigger"] == "stall"
    # Planning rounds do not count toward the 6 execution rounds:
    # 4 model calls total, 3 of them execution rounds.
    assert fake.calls == 4
    assert llm.get("iterations") == 3
    assert llm.get("terminated_by") == "model"
    # The plan reached the next execution call as the model's own message.
    last_call_messages = fake.seen_messages[-1]
    assert any("先查 A" in str(getattr(m, "content", "")) for m in last_call_messages)
    # Planning steps are not tool records: StuckDetector never sees them.
    assert not [s for s in handle.detail["steps"] if s.get("tool") == "planning"]


async def test_planning_capped_at_two_across_replan_attempts(monkeypatch):
    # Attempt 0 hits the 6-round ceiling after using both planning
    # rounds; the orchestrator replans into attempt 1, which stalls
    # twice more — but the cap is per run (task.context persists
    # across attempts), so no third planning round may occur, and the
    # stalled halt in attempt 1 ends by model answer, not by planning.
    fake = _FakeReactModel(
        [
            _inspect("/nope1.txt", "call_1"),
            _inspect("/nope2.txt", "call_2"),
            AIMessage(content="1. 查 A\n2. 查 B"),
            _inspect("/nope3.txt", "call_3"),
            _inspect("/nope4.txt", "call_4"),
            AIMessage(content="1. 查 C\n2. 查 D"),
            _inspect("/nope5.txt", "call_5"),
            _inspect("/nope6.txt", "call_6"),
            _inspect("/nope7.txt", "call_7"),
            _inspect("/nope8.txt", "call_8"),
            AIMessage(content="已有证据收尾。"),
        ]
    )
    handle = await _run(monkeypatch, fake)
    llm = handle.detail["llm"]
    steps = llm.get("planning_steps") or []
    assert len(steps) == 2
    assert not [s for s in steps if s.get("attempt") == 1]
    assert llm.get("terminated_by") == "model"
    assert llm.get("iterations") == 3  # attempt 1: two stalls + halt


async def test_planning_failure_keeps_old_plan_and_continues(monkeypatch):
    fake = _RaisingPlanningFakeModel(
        [
            _inspect("/nope1.txt", "call_1"),
            _inspect("/nope2.txt", "call_2"),
            AIMessage(content="已有证据收尾。"),
        ]
    )
    handle = await _run(monkeypatch, fake)
    llm = handle.detail["llm"]
    steps = llm.get("planning_steps") or []
    assert len(steps) == 1
    assert steps[0]["status"] == "failed"
    assert steps[0]["plan"] == []
    # The run continues and terminates normally; planning never fails it.
    assert llm.get("terminated_by") == "model"


async def test_unchanged_plan_is_no_progress_signal(monkeypatch):
    fake = _FakeReactModel(
        [
            _inspect("/nope1.txt", "call_1"),
            _inspect("/nope2.txt", "call_2"),
            _PLAN_A,
            _inspect("/nope3.txt", "call_3"),
            _inspect("/nope4.txt", "call_4"),
            _PLAN_A,
            _inspect("/nope5.txt", "call_5"),
            AIMessage(content="已有证据收尾。"),
        ]
    )
    handle = await _run(monkeypatch, fake)
    llm = handle.detail["llm"]
    steps = llm.get("planning_steps") or []
    assert len(steps) == 2
    assert steps[0]["status"] == "ok"
    assert steps[1]["status"] == "unchanged"
    assert steps[1]["changed"] is False
    assert llm.get("terminated_by") == "model"


async def test_halt_after_planning_ends_normally(monkeypatch):
    # The halt requirement is enforced by pre-emption: after two stall
    # rounds the loop plans FIRST (iteration top), and only then may
    # the model halt. The run must end by model answer with exactly
    # one planning step — planning neither blocks the halt nor forces
    # extra rounds once the plan is in.
    fake = _FakeReactModel(
        [
            _inspect("/nope1.txt", "call_1"),
            _inspect("/nope2.txt", "call_2"),
            _PLAN_A,
            AIMessage(content="按新计划收尾，不再调用。"),
        ]
    )
    handle = await _run(monkeypatch, fake)
    llm = handle.detail["llm"]
    assert len(llm.get("planning_steps") or []) == 1
    assert llm.get("terminated_by") == "model"
    assert llm.get("iterations") == 3


async def test_delegated_child_never_plans(monkeypatch):
    fake = _FakeReactModel(
        [
            _inspect("/nope1.txt", "call_1"),
            _inspect("/nope2.txt", "call_2"),
            _inspect("/nope3.txt", "call_3"),
            AIMessage(content="已有证据收尾。"),
        ]
    )
    handle = await _run(monkeypatch, fake, context={"_delegation_child": True})
    llm = handle.detail["llm"]
    assert not llm.get("planning_steps")
