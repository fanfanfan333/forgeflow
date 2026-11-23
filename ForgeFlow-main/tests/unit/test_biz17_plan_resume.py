"""BIZ-17 — a deterministic run can resume at the *next step*, not at step one.

Reported gap: "部分恢复场景尚不支持". BIZ-16 removed two blanket refusals but left
the deterministic path alone: ``claim()`` required a non-empty ``messages`` trail,
which a plan-driven run never produces. So a deterministic run could be paused
forever — its checkpoint existed, its input digests were verified, and it was
still unclaimable.

Two facts make a step-level resume defensible rather than a guess:

* progress is written **before** a step runs (``inflight=True``) and again after
  it is recorded, so a crash inside a step is refused, not replayed;
* the plan's executed sequence is hashed (``plan_signature``), so a checkpoint
  that advanced through a *different* plan is refused instead of skipping steps.

These tests pin the widening, and pin the guards that must not move with it.
"""

from __future__ import annotations

import asyncio

import pytest

import forgeflow.runtime.orchestrator as orch
import forgeflow.runtime.recovery as recovery
from forgeflow.runtime.orchestrator import RequestContext, TaskCreate
from forgeflow.runtime.recovery import RecoveryBlocked, RecoveryStore


class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, run_id: str, event: str, payload: dict | None = None) -> None:
        self.events.append((event, dict(payload or {})))

    def named(self, event: str) -> list[dict]:
        return [payload for name, payload in self.events if name == event]


def _progress(**overrides) -> dict:
    block = {
        "signature": "0" * 32,
        "next_index": 2,
        "steps": [{"index": 0, "tool": "docs.parse"}, {"index": 1, "tool": "report.render"}],
        "updated_at": 0.0,
    }
    block.update(overrides)
    return block


def _state(**overrides) -> dict:
    base = {
        "task": {"intent": "生成月度报告", "title": "t", "workflow_type": "generic"},
        "input_versions": {},
        "budget": {},
        "messages": [],
        "source_run_id": "",
        "inflight": False,
        "evaluation_only": False,
        "unsupported": "",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# 1. The predicate: one answer for the store and the route                    #
# --------------------------------------------------------------------------- #


def test_plan_progress_makes_a_messageless_checkpoint_resumable():
    state = _state(plan_progress=_progress())
    assert state["messages"] == []
    assert recovery.resumable(state) is True


def test_progress_that_never_advanced_is_not_resumable():
    # A signature alone is not evidence of work: nothing completed yet.
    state = _state(plan_progress=_progress(next_index=0, steps=[]))
    assert recovery.resumable(state) is False


def test_inflight_progress_is_still_refused():
    state = _state(plan_progress=_progress(), inflight=True)
    assert recovery.resumable(state) is False


def test_unsupported_inputs_are_still_refused():
    state = _state(
        plan_progress=_progress(),
        input_versions={"d": "unhashed:too-many-files"},
        unsupported="input cannot be verified (unhashed:too-many-files)",
    )
    assert recovery.resumable(state) is False


def test_a_message_trail_still_resumes_as_before():
    state = _state(messages=[{"type": "human", "data": {"content": "go"}}])
    assert recovery.resumable(state) is True


def test_claim_accepts_a_plan_progress_checkpoint(tmp_path):
    store = RecoveryStore(path=tmp_path / "journal.sqlite3")
    version = store.save("t1", "u1", "run-p", _state(plan_progress=_progress()), status="paused")
    claimed = store.claim("t1", "u1", "run-p", version)
    assert recovery.plan_progress(claimed)["next_index"] == 2


def test_claim_refuses_progress_that_did_not_advance(tmp_path):
    store = RecoveryStore(path=tmp_path / "journal.sqlite3")
    stalled = _state(plan_progress=_progress(next_index=0, steps=[]))
    version = store.save("t1", "u1", "run-q", stalled, status="paused")
    with pytest.raises(RecoveryBlocked, match="没有可恢复的检查点"):
        store.claim("t1", "u1", "run-q", version)


# --------------------------------------------------------------------------- #
# 2. The signature and the bounded history                                    #
# --------------------------------------------------------------------------- #


def test_plan_signature_is_stable_but_sequence_sensitive():
    first = [
        {"step_id": "s1", "tool": "docs.parse", "step_type": "agent"},
        {"step_id": "s2", "tool": "report.render", "step_type": "agent"},
    ]
    assert recovery.plan_signature(first) == recovery.plan_signature(list(first))
    # Notes are prose, not sequence.
    with_notes = [dict(item, note="换一种说法") for item in first]
    assert recovery.plan_signature(with_notes) == recovery.plan_signature(first)
    # Order and membership are the sequence.
    assert recovery.plan_signature(list(reversed(first))) != recovery.plan_signature(first)
    assert recovery.plan_signature(first[:1]) != recovery.plan_signature(first)


def test_oversized_progress_flags_observations_instead_of_dropping_them():
    rows = [
        {"index": 0, "tool": "docs.parse", "observation": {"text": "x" * 400_000}},
        {"index": 1, "tool": "report.render", "status": "ok"},
    ]
    bounded = recovery._bounded_steps(rows)
    assert bounded[0]["observation"]["truncated"] is True
    assert bounded[0]["tool"] == "docs.parse"  # the step stays identifiable
    assert bounded[1] == rows[1]  # a small row is copied verbatim


def test_small_progress_keeps_the_observation_verbatim():
    rows = [{"index": 0, "tool": "docs.parse", "observation": {"text": "tiny"}}]
    assert recovery._bounded_steps(rows) == rows


# --------------------------------------------------------------------------- #
# 3. Resuming must not switch executors                                       #
# --------------------------------------------------------------------------- #


def test_a_deterministic_checkpoint_pins_the_deterministic_executor():
    assert orch._resume_runtime_mode("react", _state(plan_progress=_progress())) == "deterministic"
    assert orch._resume_runtime_mode("llm", _state(plan_progress=_progress())) == "deterministic"
    # No plan progress ⇒ the configured mode stands.
    assert orch._resume_runtime_mode("react", _state()) == "react"
    assert orch._resume_runtime_mode("deterministic", None) == "deterministic"


# --------------------------------------------------------------------------- #
# 4. The real loop: completed steps are not executed twice                    #
# --------------------------------------------------------------------------- #


def _plan(monkeypatch) -> list[str]:
    monkeypatch.setattr(
        orch,
        "_DEFAULT_STEPS",
        [
            {"tool": "docs.parse", "step_type": "agent", "note": "解析文本"},
            {"tool": "report.render", "step_type": "agent", "note": "生成报告"},
        ],
    )
    return ["docs.parse", "report.render"]


def test_a_deterministic_run_resumes_at_the_next_step(tmp_path, monkeypatch):
    tools = _plan(monkeypatch)
    store = RecoveryStore(path=tmp_path / "journal.sqlite3")
    ctx = RequestContext(tenant_id="t-biz17", user_id="u-biz17", role="sales_rep")
    run_id = "run-biz17"

    # --- attempt 1: a real run that writes real progress ---------------------
    store.save("t-biz17", "u-biz17", run_id, _state(), status="active")
    token = recovery.current_recovery.set((store, "t-biz17", "u-biz17", run_id))
    try:
        bus_one = _Bus()
        steps_one, errors_one = asyncio.run(
            orch._default_executor(TaskCreate(intent="生成月度报告"), ctx, bus_one, run_id)
        )
    finally:
        recovery.current_recovery.reset(token)

    assert errors_one == []
    assert [step["tool"] for step in steps_one] == tools
    checkpoint = store.get("t-biz17", "u-biz17", run_id)["state"]
    progress = recovery.plan_progress(checkpoint)
    assert progress is not None
    assert progress["next_index"] == len(tools)
    assert [row["tool"] for row in progress["steps"]] == tools

    # --- a crash *between* step 1 and step 2, at a real step boundary --------
    progress["steps"] = progress["steps"][:1]
    progress["next_index"] = 1
    checkpoint["inflight"] = False
    checkpoint["tool_invocations"] = list(checkpoint["tool_invocations"])[:1]
    store.save("t-biz17", "u-biz17", run_id, checkpoint, status="paused")

    # --- attempt 2: resume ---------------------------------------------------
    resumed = TaskCreate(intent="生成月度报告")
    resumed._recovery_state = store.get("t-biz17", "u-biz17", run_id)["state"]
    resumed.context["tool_invocations"] = list(resumed._recovery_state["tool_invocations"])
    token = recovery.current_recovery.set((store, "t-biz17", "u-biz17", run_id))
    try:
        bus_two = _Bus()
        steps_two, errors_two = asyncio.run(orch._default_executor(resumed, ctx, bus_two, run_id))
    finally:
        recovery.current_recovery.reset(token)

    assert errors_two == []
    # The restored payload is the *same* record, not a re-execution.
    assert steps_two[0] == steps_one[0]
    assert [step["tool"] for step in steps_two] == tools
    # Exactly one new invocation, and it is the second step.
    assert [row["tool"] for row in resumed.context["tool_invocations"]] == tools
    assert [payload["index"] for payload in bus_two.named("run.step")] == [1]
    assert any(
        payload.get("reason") == "recovery_resumed" for payload in bus_two.named("run.warning")
    )


def test_a_changed_plan_refuses_to_resume_instead_of_skipping_steps(tmp_path, monkeypatch):
    _plan(monkeypatch)
    store = RecoveryStore(path=tmp_path / "journal.sqlite3")
    ctx = RequestContext(tenant_id="t-biz17b", user_id="u-biz17b", role="sales_rep")
    run_id = "run-biz17b"
    stale = _state(
        plan_progress=_progress(
            signature="f" * 32, next_index=1, steps=[{"index": 0, "tool": "crm_pipeline_summary"}]
        )
    )
    store.save("t-biz17b", "u-biz17b", run_id, stale, status="paused")

    task = TaskCreate(intent="生成月度报告")
    task._recovery_state = store.get("t-biz17b", "u-biz17b", run_id)["state"]
    bus = _Bus()
    token = recovery.current_recovery.set((store, "t-biz17b", "u-biz17b", run_id))
    try:
        steps, errors = asyncio.run(orch._default_executor(task, ctx, bus, run_id))
    finally:
        recovery.current_recovery.reset(token)

    assert steps == []
    assert not task.context.get("tool_invocations")  # nothing was executed
    assert errors and "计划已变化" in errors[0]
    assert bus.named("run.error")[0]["reason"] == "recovery_plan_mismatch"
    # The progress is invalidated so the route cannot advertise a resume.
    after = store.get("t-biz17b", "u-biz17b", run_id)["state"]
    assert recovery.resumable(after) is False
