"""Phase 9 A4 — approver edit / respond tiers for confirm_permission.

An approver may (a) edit the staged args — the stored hash follows the
EDITED args, so the gate verifies the edited call and never the
original; or (b) answer instead of executing — the human text replaces
execution. Both are confirm_permission-only and fail closed otherwise.
"""

from __future__ import annotations

import pytest

import forgeflow.hitl.pending as P
import forgeflow.runtime.approval_policy as approval_policy
from forgeflow.runtime import tool_registry
from forgeflow.runtime.approval_policy import hash_approval_args
from forgeflow.runtime.tool_executor import ToolCallContext, ToolExecutor
from forgeflow.runtime.tool_registry import ToolBinding

_TENANT = "t-edit"
_OWNER = "u-owner"
_OTHER = "u-other"


@pytest.fixture(autouse=True)
def _fresh_store():
    store = P.InMemoryPendingActionStore()
    P.set_pending_store(store)
    yield store
    P.reset_pending_store()


@pytest.fixture(autouse=True)
def _clean_registry():
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()
    yield
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()
    approval_policy.unregister_pre_approval_tool("mock.transfer")


@pytest.fixture()
def _gated_tool():
    async def _handler(args: dict, ctx: object) -> dict:
        return {"ok": True, "transferred": True}

    tool_registry.register(
        ToolBinding(
            tool_id="mock.transfer",
            handler=_handler,
            kind="real",
            provider="test",
            description="mock irreversible tool",
        )
    )
    approval_policy.register_pre_approval_tool("mock.transfer")
    return "mock.transfer"


def _ctx(tool_args: dict | None = None, **kw) -> ToolCallContext:
    base = dict(
        run_id="run-edit-1",
        step_id="run-edit-1:0:0",
        tenant_id=_TENANT,
        user_id=_OWNER,
        role="admin",
        intent="转一笔钱",
        attempt=0,
        args=dict(tool_args or {}),
    )
    base.update(kw)
    return ToolCallContext(**base)


def _task_context(records: list) -> object:
    class _Task:
        context = {"tool_invocations": records}

    return _Task()


def _staged_action(store, args: dict):
    return store.create(
        _TENANT,
        run_id="run-edit-1",
        kind=P.CONFIRM_PERMISSION,
        payload={
            "tool": "mock.transfer",
            "args": dict(args),
            "args_hash": hash_approval_args(args),
            "owner": _OWNER,
        },
    )


def test_edit_rehashes_staged_args(_fresh_store) -> None:
    action = _staged_action(_fresh_store, {"amount": 100})
    edited = {"amount": 200}
    resolved = _fresh_store.resolve(
        _TENANT, action.pending_id, decision="edit", actor=_OWNER, edited_args=edited
    )
    assert resolved.resolution == "edit"
    assert resolved.payload["args"] == edited
    assert resolved.payload["args_hash"] == hash_approval_args(edited)
    assert resolved.payload["edited_from_hash"] == hash_approval_args({"amount": 100})


def test_edit_rejected_for_wrong_kind_or_missing_args(_fresh_store) -> None:
    clarify = _fresh_store.create(_TENANT, run_id="run-edit-1", kind=P.CLARIFY, payload={})
    with pytest.raises(P.PendingActionInvalidDecision):
        _fresh_store.resolve(_TENANT, clarify.pending_id, decision="edit", actor=_OWNER, edited_args={"x": 1})
    action = _staged_action(_fresh_store, {"amount": 100})
    with pytest.raises(P.PendingActionInvalidDecision):
        _fresh_store.resolve(_TENANT, action.pending_id, decision="edit", actor=_OWNER)
    with pytest.raises(P.PendingActionInvalidDecision):
        _fresh_store.resolve(_TENANT, action.pending_id, decision="explode", actor=_OWNER)


def test_respond_records_answer_and_requires_text(_fresh_store) -> None:
    action = _staged_action(_fresh_store, {"amount": 100})
    with pytest.raises(P.PendingActionInvalidDecision):
        _fresh_store.resolve(_TENANT, action.pending_id, decision="respond", actor=_OWNER)
    resolved = _fresh_store.resolve(
        _TENANT, action.pending_id, decision="respond", actor=_OWNER, response_text="已线下转账"
    )
    assert resolved.resolution == "respond"
    assert resolved.payload["response_text"] == "已线下转账"


def test_edit_is_idempotent_first_resolution_wins(_fresh_store) -> None:
    action = _staged_action(_fresh_store, {"amount": 100})
    first = _fresh_store.resolve(
        _TENANT, action.pending_id, decision="edit", actor=_OWNER, edited_args={"amount": 200}
    )
    second = _fresh_store.resolve(_TENANT, action.pending_id, decision="approve", actor=_OWNER)
    assert second.resolution == "edit"
    assert second.payload["args"] == {"amount": 200}
    assert first.payload["args_hash"] == second.payload["args_hash"]


def test_effective_args_forces_edited_args_over_model() -> None:
    from forgeflow.runtime.orchestrator import RequestContext, TaskCreate
    from forgeflow.runtime.react_executor import ReactExecutor

    task = TaskCreate(
        intent="转一笔钱",
        context={
            "_pending_approval": {"mock.transfer": "pa-9"},
            "_pending_approval_args": {"mock.transfer": {"amount": 200}},
        },
    )
    ctx = RequestContext(tenant_id=_TENANT, user_id=_OWNER, role="admin")
    args, _ = ReactExecutor()._effective_args("mock.transfer", {"amount": 999}, task, ctx, [])
    assert args["amount"] == 200
    assert args["pending_id"] == "pa-9"


@pytest.mark.asyncio
async def test_edited_grant_executes_and_original_args_refused(_gated_tool, _fresh_store) -> None:
    records: list = []
    blocked = await ToolExecutor().execute(
        _gated_tool, ctx=_ctx({"amount": 100}, task_context=_task_context(records))
    )
    pending_id = blocked.payload["pending_id"]
    _fresh_store.resolve(
        _TENANT, pending_id, decision="edit", actor=_OWNER, edited_args={"amount": 200}
    )
    # The ORIGINAL args no longer verify against the edited hash.
    stale = await ToolExecutor().execute(
        _gated_tool,
        ctx=_ctx({"amount": 100, "pending_id": pending_id}, task_context=_task_context(records)),
    )
    assert stale.status == "refused"
    assert stale.executed is False
    # The EDITED args verify and execute exactly once.
    granted = await ToolExecutor().execute(
        _gated_tool,
        ctx=_ctx({"amount": 200, "pending_id": pending_id}, task_context=_task_context(records)),
    )
    assert granted.status == "ok"
    assert granted.executed is True
    assert granted.approval_id == pending_id


@pytest.mark.asyncio
async def test_edited_grant_matches_when_the_loop_injects_platform_args(
    _gated_tool, _fresh_store
) -> None:
    """The real loop always adds intent/query/observations around the call.

    The edit hash covers only the approver's args. Those platform keys
    must not turn a valid edit into an argument-hash mismatch.
    """
    from forgeflow.runtime.orchestrator import RequestContext, TaskCreate
    from forgeflow.runtime.react_executor import ReactExecutor

    seen: dict = {}

    async def _handler(args: dict, ctx: object) -> dict:
        seen["amount"] = args.get("amount")
        return {"ok": True, "transferred": True}

    tool_registry.register(
        ToolBinding(
            tool_id="mock.transfer",
            handler=_handler,
            kind="real",
            provider="test",
            description="mock irreversible tool",
        )
    )
    blocked = await ToolExecutor().execute(
        _gated_tool, ctx=_ctx({"amount": 100})
    )
    pending_id = blocked.payload["pending_id"]
    _fresh_store.resolve(
        _TENANT, pending_id, decision="edit", actor=_OWNER, edited_args={"amount": 200}
    )
    task = TaskCreate(
        intent="转一笔钱",
        context={
            "_pending_approval": {_gated_tool: pending_id},
            "_pending_approval_args": {_gated_tool: {"amount": 200}},
        },
    )
    request_ctx = RequestContext(tenant_id=_TENANT, user_id=_OWNER, role="admin")
    args, _injected = ReactExecutor()._effective_args(
        _gated_tool, {"amount": 100, "pending_id": "pa-forged"}, task, request_ctx, []
    )
    assert args["amount"] == 200
    assert "intent" in args and "observations" in args
    granted = await ToolExecutor().execute(
        _gated_tool,
        ctx=_ctx(
            args,
            run_id="run-edit-resume",
            step_id="run-edit-resume:0:0",
            task_context=task,
        ),
    )
    assert granted.status == "ok", granted.error
    assert granted.executed is True
    assert seen["amount"] == 200
