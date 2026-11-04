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
