"""Pre-write checkpoint persistence + round-checkpoint approval state.

Regression coverage for two linked checkpoint flaws:

* The react loop only checkpointed AFTER a whole tool batch, and
  the round state hardcoded ``pending_writes=[]`` — a crash mid-batch
  left a clean cursor and the resume replayed the batch's side
  effects. Now a write-class tool's pending write (with its
  idempotency key) is persisted before execution, the key is handed
  to the tool, and the entry is confirmed only on a definitive
  outcome.
* The round state hardcoded ``approval=None``, which made
  ``verify_resume``'s approval re-verification branch dead code for
  round checkpoints. The state now derives approval from the run's
  real approval signals.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from forgeflow.runtime import tool_registry
from forgeflow.runtime.checkpoints import (
    InMemoryCheckpointStore,
    pending_write_entry,
    reset_checkpoint_store,
    set_checkpoint_store,
)
from forgeflow.runtime.react_executor import (
    _is_write_class_tool,
    _round_checkpoint_state,
    _save_round_checkpoint,
)
from forgeflow.runtime.tool_executor import ToolCallContext, ToolExecutor
from forgeflow.runtime.tool_registry import ToolBinding


@pytest.fixture(autouse=True)
def _clean_registry():
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()
    yield
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()


def _task(context=None):
    return SimpleNamespace(intent="do it", context=dict(context or {}))


def _ctx():
    return SimpleNamespace(tenant_id="t1", user_id="u1", role="admin")


def _state(runtime_meta=None, records=None, context=None):
    return _round_checkpoint_state(
        _task(context),
        [],
        runtime_meta or {},
        2,
        records or [],
        None,
        _ctx(),
    )


# --- write-class classification ---------------------------------------------- #


def test_write_class_classification():
    assert _is_write_class_tool("textfile.edit") is True
    assert _is_write_class_tool("document.edit") is True
    assert _is_write_class_tool("code.execute") is True
    assert _is_write_class_tool("code.commit") is True
    assert _is_write_class_tool("textfile.inspect") is False
    assert _is_write_class_tool("research.search") is False
    assert _is_write_class_tool("evidence.read") is False
    # Terminal rendering is the run's own answer, not a replayable write.
    assert _is_write_class_tool("report.render") is False


# --- round state: pending writes ---------------------------------------------- #


def test_round_state_carries_pending_write_ledger_copies():
    entry = pending_write_entry("run-1", 2, "call-9", "textfile.edit", args_hash="h")
    state = _state(runtime_meta={"pending_writes": [entry]})
    assert state["pending_writes"] == [entry]
    # The state holds copies: later loop mutation must not rewrite
    # what was persisted.
    entry["confirmed"] = True
    assert state["pending_writes"][0]["confirmed"] is False


def test_round_state_pending_writes_default_empty():
    assert _state()["pending_writes"] == []


# --- round state: approval derivation ------------------------------------------ #


def test_round_state_approval_from_awaiting_record():
    records = [
        {
            "tool": "payment.transfer",
            "status": "awaiting_approval",
            "arguments_hash": "hash-1",
            "payload": {"pending_id": "pa-1"},
        }
    ]
    state = _state(records=records)
    assert state["approval"] == {
        "pending_id": "pa-1",
        "owner": "u1",
        "args_hash": "hash-1",
    }


def test_round_state_approval_from_hitl_halt_meta():
    state = _state(
        runtime_meta={
            "pending_approval": {"pending_id": "pa-2", "tool": "x", "args_hash": "hash-2"}
        }
    )
    assert state["approval"]["pending_id"] == "pa-2"
    assert state["approval"]["args_hash"] == "hash-2"


def test_round_state_approval_from_context_grant_mapping():
    records = [{"tool": "payment.transfer", "status": "ok", "arguments_hash": "hash-3", "payload": {}}]
    state = _state(records=records, context={"_pending_approval": {"payment.transfer": "pa-3"}})
    assert state["approval"]["pending_id"] == "pa-3"
    assert state["approval"]["args_hash"] == "hash-3"


def test_round_state_approval_none_when_no_approval_anywhere():
    assert _state()["approval"] is None


# --- pre-write checkpoint actually persists before execution ------------------- #


def test_save_round_checkpoint_persists_unconfirmed_pending_write():
    store = InMemoryCheckpointStore()
    set_checkpoint_store(store)
    try:
        entry = pending_write_entry("run-9", 1, "call-1", "code.execute", args_hash="h")
        _save_round_checkpoint(
            _task(),
            _ctx(),
            "run-9",
            [],
            {"pending_writes": [entry]},
            1,
            [],
            None,
        )
        latest = store.latest("t1", "run-9")
        assert latest is not None
        assert latest.state["pending_writes"][0]["idempotency_key"] == "run-9:1:call-1"
        assert latest.state["pending_writes"][0]["confirmed"] is False
    finally:
        reset_checkpoint_store()


# --- idempotency key wiring into the tool -------------------------------------- #


async def test_tool_receives_idempotency_key_without_polluting_args():
    seen: dict = {}

    async def _handler(args: dict, ctx: object) -> dict:
        seen.update(args)
        return {"ok": True}

    tool_registry.register(
        ToolBinding(
            tool_id="mock.write",
            handler=_handler,
            kind="real",
            provider="test",
            description="mock write tool",
        )
    )
    ctx = ToolCallContext(
        run_id="run-1",
        step_id="run-1:0:0",
        tenant_id="t1",
        user_id="u1",
        role="admin",
        args={"amount": 5},
        idempotency_key="run-1:2:call-9",
    )
    invocation = await ToolExecutor().execute("mock.write", ctx=ctx)
    assert invocation.status == "ok"
    # The handler saw the key (so a key-honoring tool can dedupe)…
    assert seen.get("idempotency_key") == "run-1:2:call-9"
    assert seen.get("amount") == 5
    # …but the call's own args (and therefore its arguments_hash) are
    # untouched by the wiring.
    assert ctx.args == {"amount": 5}
    plain_ctx = ToolCallContext(
        run_id="run-1",
        step_id="run-1:0:0",
        tenant_id="t1",
        user_id="u1",
        role="admin",
        args={"amount": 5},
    )
    plain = await ToolExecutor().execute("mock.write", ctx=plain_ctx)
    assert plain.arguments_hash == invocation.arguments_hash


async def test_mcp_tool_args_do_not_receive_idempotency_key():
    """MCP servers validate arguments strictly against their schema
    and reject unknown keys — for ``mcp.*`` tools the key must stay
    on the context only (regression: wiring the key into args broke
    a real MCP call with ``unexpected_keyword_argument``)."""
    seen: dict = {}

    async def _handler(args: dict, ctx: object) -> dict:
        seen.update(args)
        seen["ctx_key"] = ctx.idempotency_key
        return {"ok": True}

    tool_registry.register(
        ToolBinding(
            tool_id="mcp.internal.sample_write",
            handler=_handler,
            kind="real",
            provider="mcp:internal",
            description="mock mcp write tool",
        )
    )
    ctx = ToolCallContext(
        run_id="run-1",
        step_id="run-1:0:0",
        tenant_id="t1",
        user_id="u1",
        role="admin",
        args={},
        idempotency_key="run-1:0:call-1",
    )
    invocation = await ToolExecutor().execute("mcp.internal.sample_write", ctx=ctx)
    assert invocation.status == "ok"
    assert "idempotency_key" not in seen
    assert seen["ctx_key"] == "run-1:0:call-1"


# --- pre-write persistence failure blocks the write (end-to-end) -------------- #


class _PreWriteFailingStore(InMemoryCheckpointStore):
    """Fails exactly the saves carrying an UNCONFIRMED pending write —
    i.e. the pre-write persistence — and delegates every other save.

    Simulates a checkpoint-backend outage at the one moment the
    ledger must land: before the fix, ``_save_round_checkpoint``
    swallowed this failure and the write executed anyway, leaving the
    previous clean checkpoint (``pending_writes`` empty) as the
    latest, so ``verify_resume`` waved a resume through and an
    interruption could replay the write.
    """

    def save(self, tenant, *, run_id, round, kind, state):  # noqa: A002 — store API shape
        from forgeflow.runtime.checkpoints import _write_confirmed

        entries = (state or {}).get("pending_writes") or []
        if any(isinstance(e, dict) and not _write_confirmed(e) for e in entries):
            raise RuntimeError("simulated checkpoint store failure")
        return super().save(tenant, run_id=run_id, round=round, kind=kind, state=state)


async def test_pre_write_checkpoint_failure_blocks_write_and_resume_does_not_replay(
    monkeypatch, force_memory_backend
):
    """End-to-end through the react loop (inc17 scripted-model harness):

    * the pre-write checkpoint save raises → the write-class tool
      must NOT execute and the model must get an error ToolMessage
      explaining the block;
    * afterwards, resume verification against the latest persisted
      checkpoint must NOT refuse for a phantom unconfirmed write
      (the blocked entry is confirmed/blocked), and the write must
      still never have run — no replay.
    """
    from langchain_core.messages import ToolMessage

    from forgeflow.runtime.checkpoints import _write_confirmed, verify_resume
    from forgeflow.runtime.orchestrator import (
        RequestContext,
        TaskCreate,
        reset_run_store,
        run_task,
    )
    from tests.unit.test_inc17_react_loop import (
        _patch_models,
        _patch_settings,
        _RecordingBus,
        _ScriptedModel,
    )

    invocations: list[dict] = []

    async def _write_handler(args, ctx):  # noqa: ANN001, ANN202
        invocations.append(dict(args))
        return {"ok": True, "written": True}

    tool_registry.register(
        ToolBinding(
            tool_id="data.query",
            handler=_write_handler,
            kind="real",
            provider="test",
            description="counting write probe",
        )
    )
    store = _PreWriteFailingStore()
    set_checkpoint_store(store)
    reset_run_store()
    try:
        _patch_settings(monkeypatch, mode="react", provider="mock")
        fake = _ScriptedModel(
            [
                {
                    "content": "执行写入",
                    "tool_calls": [{"name": "data.query", "args": {"table": "t"}, "id": "w1"}],
                },
                {"content": "最终答复：写入未完成", "tool_calls": []},
            ]
        )
        _patch_models(monkeypatch, fake)
        task = TaskCreate(intent="查询并记录数据", workflow_type="generic", context={})
        bus = _RecordingBus()
        await run_task(
            task,
            RequestContext(tenant_id="t-inc17", user_id="u-1", role="admin"),
            run_id="run-prewrite-block",
            bus=bus,
        )

        # (a) The write tool's handler was NEVER invoked, and no
        # execution record exists for it.
        assert invocations == []
        assert not [
            r for r in task.context.get("tool_invocations", []) if r.get("tool") == "data.query"
        ]

        # (b) The model received an error ToolMessage for that call
        # explaining the block.
        assert len(fake.calls) >= 2
        blocked_msgs = [
            m
            for m in fake.calls[1]
            if isinstance(m, ToolMessage) and m.tool_call_id == "w1"
        ]
        assert blocked_msgs, "model must be told the write was blocked"
        assert "未执行" in blocked_msgs[0].content
        assert "检查点" in blocked_msgs[0].content
        warns = [
            d
            for t, d in bus.events
            if t == "run.warning" and d.get("reason") == "pre_write_checkpoint_failed"
        ]
        assert warns and warns[0]["tool"] == "data.query"

        # Resume half: the latest persisted checkpoint (written by the
        # post-batch save, which succeeded) carries the blocked entry
        # as confirmed/blocked — no unconfirmed pending writes remain,
        # so verify_resume passes instead of refusing for a phantom
        # in-flight write, and nothing replays the write.
        latest = store.latest("t-inc17", "run-prewrite-block")
        assert latest is not None
        entries = [
            e for e in latest.state.get("pending_writes") or [] if e.get("tool") == "data.query"
        ]
        assert entries, "blocked entry must be carried in the persisted ledger"
        assert all(
            e.get("confirmed") is True and e.get("status") == "blocked" for e in entries
        )
        assert all(
            _write_confirmed(e) for e in latest.state.get("pending_writes") or []
        )
        state = verify_resume(latest, run_status="interrupted")
        assert state == latest.state
        assert invocations == []
    finally:
        reset_checkpoint_store()
        reset_run_store()
