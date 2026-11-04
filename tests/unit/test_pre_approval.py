"""Phase 4 B-track — pre-execution approval gate + owner binding.

Pins the gate in :class:`forgeflow.runtime.tool_executor.ToolExecutor`
and the owner binding in :mod:`forgeflow.hitl.pending`:

* a gated tool never executes without a verified grant — the first
  call pauses (``awaiting_approval``) and stages exactly one
  ``confirm_permission`` action, never a duplicate;
* approving then resuming with the grant executes once, and the
  record carries the grant id (success consumes it — a second
  success with the same grant is refused);
* rejection stands for identical args (no approval spam);
* a grant for other args, another user, or another run is refused;
* resolving an owner-carrying action as a non-owner is refused.
"""

import pytest

import forgeflow.hitl.pending as P
from forgeflow.runtime import approval_policy, tool_registry
from forgeflow.runtime.tool_executor import ToolCallContext, ToolExecutor
from forgeflow.runtime.tool_registry import ToolBinding

_TENANT = "t-pre"
_OWNER = "u-owner"
_OTHER = "u-other"


@pytest.fixture(autouse=True)
def _fresh_store():
    """Pin a fresh in-memory pending store per test (no DB, offline)."""
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
    """A bound mock tool under pre-approval (the gate's test stand-in)."""

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
        run_id="run-pre-1",
        step_id="run-pre-1:0:0",
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
    """Stand-in for the react loop's task: records live on .context."""

    class _Task:
        context = {"tool_invocations": records}

    return _Task()


# --------------------------------------------------------------------------- #
# Policy                                                                      #
# --------------------------------------------------------------------------- #


def test_policy_gates_narrow_grant_tools_only() -> None:
    for tool in (
        "payment.transfer",
        "payment.refund",
        "data.export",
        "policy.grant",
        "skill.publish",
    ):
        assert approval_policy.requires_pre_approval(tool) is True
    assert approval_policy.requires_pre_approval("research.search") is False
    assert approval_policy.requires_pre_approval("document.edit") is False


def test_policy_registry_add_remove() -> None:
    assert approval_policy.requires_pre_approval("mock.transfer") is False
    approval_policy.register_pre_approval_tool("mock.transfer")
    assert approval_policy.requires_pre_approval("mock.transfer") is True
    approval_policy.unregister_pre_approval_tool("mock.transfer")
    assert approval_policy.requires_pre_approval("mock.transfer") is False


# --------------------------------------------------------------------------- #
# Gate: block / reuse / approve / reject / mismatch / owner / consumed        #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_gated_call_blocks_and_stages_one_action(_gated_tool, _fresh_store) -> None:
    inv = await ToolExecutor().execute(_gated_tool, ctx=_ctx({"amount": 100}))
    assert inv.status == "awaiting_approval"
    assert inv.executed is False
    assert inv.invoked is False
    assert inv.payload["pending_id"]
    actions = _fresh_store.list(_TENANT, run_id="run-pre-1")
    assert len(actions) == 1
    assert actions[0].kind == P.CONFIRM_PERMISSION
    assert actions[0].status == P.WAITING
    assert actions[0].payload["tool"] == _gated_tool
    assert actions[0].payload["owner"] == _OWNER


@pytest.mark.asyncio
async def test_duplicate_bare_call_reuses_waiting_action(_gated_tool, _fresh_store) -> None:
    first = await ToolExecutor().execute(_gated_tool, ctx=_ctx({"amount": 100}))
    second = await ToolExecutor().execute(_gated_tool, ctx=_ctx({"amount": 100}))
    assert second.status == "awaiting_approval"
    assert second.payload["pending_id"] == first.payload["pending_id"]
    assert len(_fresh_store.list(_TENANT, run_id="run-pre-1")) == 1


@pytest.mark.asyncio
async def test_approved_grant_executes_once_then_consumed(_gated_tool, _fresh_store) -> None:
    records: list = []
    blocked = await ToolExecutor().execute(
        _gated_tool, ctx=_ctx({"amount": 100}, task_context=_task_context(records))
    )
    pending_id = blocked.payload["pending_id"]
    _fresh_store.resolve(_TENANT, pending_id, decision="approve", actor=_OWNER)
    granted = await ToolExecutor().execute(
        _gated_tool,
        ctx=_ctx({"amount": 100, "pending_id": pending_id}, task_context=_task_context(records)),
    )
    assert granted.status == "ok"
    assert granted.executed is True
    assert granted.approval_id == pending_id
    records.append(granted.to_dict())
    replay = await ToolExecutor().execute(
        _gated_tool,
        ctx=_ctx({"amount": 100, "pending_id": pending_id}, task_context=_task_context(records)),
    )
    assert replay.status == "refused"
    assert replay.executed is False
    assert "已使用过" in (replay.error or "")


@pytest.mark.asyncio
async def test_rejection_stands_for_identical_args(_gated_tool, _fresh_store) -> None:
    blocked = await ToolExecutor().execute(_gated_tool, ctx=_ctx({"amount": 100}))
    pending_id = blocked.payload["pending_id"]
    _fresh_store.resolve(_TENANT, pending_id, decision="reject", actor=_OWNER)
    retry = await ToolExecutor().execute(_gated_tool, ctx=_ctx({"amount": 100}))
    assert retry.status == "refused"
    assert retry.executed is False
    assert len(_fresh_store.list(_TENANT, run_id="run-pre-1")) == 1


@pytest.mark.asyncio
async def test_grant_for_other_args_is_refused(_gated_tool, _fresh_store) -> None:
    blocked = await ToolExecutor().execute(_gated_tool, ctx=_ctx({"amount": 100}))
    pending_id = blocked.payload["pending_id"]
    _fresh_store.resolve(_TENANT, pending_id, decision="approve", actor=_OWNER)
    other = await ToolExecutor().execute(
        _gated_tool, ctx=_ctx({"amount": 200, "pending_id": pending_id})
    )
    assert other.status == "refused"
    assert other.executed is False


@pytest.mark.asyncio
async def test_grant_for_other_user_is_refused(_gated_tool, _fresh_store) -> None:
    blocked = await ToolExecutor().execute(_gated_tool, ctx=_ctx({"amount": 100}))
    pending_id = blocked.payload["pending_id"]
    _fresh_store.resolve(_TENANT, pending_id, decision="approve", actor=_OWNER)
    other = await ToolExecutor().execute(
        _gated_tool, ctx=_ctx({"amount": 100, "pending_id": pending_id}, user_id=_OTHER)
    )
    assert other.status == "refused"
    assert other.executed is False


@pytest.mark.asyncio
async def test_grant_for_other_run_is_refused(_gated_tool, _fresh_store) -> None:
    blocked = await ToolExecutor().execute(_gated_tool, ctx=_ctx({"amount": 100}))
    pending_id = blocked.payload["pending_id"]
    _fresh_store.resolve(_TENANT, pending_id, decision="approve", actor=_OWNER)
    other = await ToolExecutor().execute(
        _gated_tool,
        ctx=_ctx(
            {"amount": 100, "pending_id": pending_id}, run_id="run-pre-2", step_id="run-pre-2:0:0"
        ),
    )
    assert other.status == "refused"
    assert other.executed is False


@pytest.mark.asyncio
async def test_same_run_approved_grant_reuses_without_presented_id(
    _gated_tool, _fresh_store
) -> None:
    blocked = await ToolExecutor().execute(_gated_tool, ctx=_ctx({"amount": 100}))
    pending_id = blocked.payload["pending_id"]
    _fresh_store.resolve(_TENANT, pending_id, decision="approve", actor=_OWNER)
    # No pending_id presented: the same run re-issuing the identical
    # call finds its own approved grant (a new run would not).
    granted = await ToolExecutor().execute(_gated_tool, ctx=_ctx({"amount": 100}))
    assert granted.status == "ok"
    assert granted.approval_id == pending_id


@pytest.mark.asyncio
async def test_non_gated_tool_unaffected(_fresh_store) -> None:
    inv = await ToolExecutor().execute("docs.parse", ctx=_ctx({"text": "第一段\n\n第二段"}))
    assert inv.status == "ok"
    assert inv.executed is True


# --------------------------------------------------------------------------- #
# Owner binding on resolve                                                    #
# --------------------------------------------------------------------------- #


def test_resolve_owner_binding(_fresh_store) -> None:
    action = _fresh_store.create(
        _TENANT,
        run_id="run-pre-1",
        kind=P.CONFIRM_PERMISSION,
        payload={"tool": "mock.transfer", "owner": _OWNER},
    )
    with pytest.raises(P.PendingActionOwnerMismatch):
        _fresh_store.resolve(_TENANT, action.pending_id, decision="approve", actor=_OTHER)
    resolved = _fresh_store.resolve(_TENANT, action.pending_id, decision="approve", actor=_OWNER)
    assert resolved.status == P.RESOLVED
    assert resolved.resolved_by == _OWNER


def test_resolve_without_owner_keeps_tenant_only_behaviour(_fresh_store) -> None:
    action = _fresh_store.create(_TENANT, run_id="run-pre-1", kind=P.CLARIFY, payload={})
    resolved = _fresh_store.resolve(_TENANT, action.pending_id, decision="approve", actor=_OTHER)
    assert resolved.status == P.RESOLVED


# --------------------------------------------------------------------------- #
# Resume injection + endpoint                                                 #
# --------------------------------------------------------------------------- #


def test_effective_args_injects_platform_pending_id() -> None:
    from forgeflow.runtime.orchestrator import RequestContext, TaskCreate
    from forgeflow.runtime.react_executor import ReactExecutor

    task = TaskCreate(intent="转一笔钱", context={"_pending_approval": {"mock.transfer": "pa-9"}})
    ctx = RequestContext(tenant_id=_TENANT, user_id=_OWNER, role="admin")
    args, injected = ReactExecutor()._effective_args(
        "mock.transfer", {"amount": 100, "pending_id": "pa-fake"}, task, ctx, []
    )
    assert args["pending_id"] == "pa-9"
    assert "pending_id" in injected


def test_effective_args_pops_model_pending_id_without_grant() -> None:
    from forgeflow.runtime.orchestrator import RequestContext, TaskCreate
    from forgeflow.runtime.react_executor import ReactExecutor

    task = TaskCreate(intent="转一笔钱", context={})
    ctx = RequestContext(tenant_id=_TENANT, user_id=_OWNER, role="admin")
    args, _ = ReactExecutor()._effective_args(
        "mock.transfer", {"amount": 100, "pending_id": "pa-fake"}, task, ctx, []
    )
    assert "pending_id" not in args


@pytest.mark.asyncio
async def test_resume_endpoint_creates_run_with_grant(_fresh_store, monkeypatch) -> None:
    import forgeflow.api.routers.pending_actions as pa_router
    from forgeflow.rbac.models import UserContext
    from forgeflow.runtime.orchestrator import RunHandle

    action = _fresh_store.create(
        _TENANT,
        run_id="run-pre-1",
        kind=P.CONFIRM_PERMISSION,
        payload={"tool": "mock.transfer", "owner": _OWNER, "args_hash": "x"},
    )
    _fresh_store.resolve(_TENANT, action.pending_id, decision="approve", actor=_OWNER)

    class _Record:
        intent = "转一笔钱"
        workflow_type = "generic"
        declared_inputs = {"x": 1}

    seen: dict = {}

    async def _fake_header(tenant: str, run_id: str) -> object:
        assert (tenant, run_id) == (_TENANT, "run-pre-1")
        return _Record()

    async def _fake_run_task(task: object, ctx: object) -> object:
        seen["task"] = task
        return RunHandle(run_id="run-new", thread_id="th-new", status="running")

    monkeypatch.setattr(pa_router, "load_run_header", _fake_header)
    monkeypatch.setattr(pa_router, "run_task", _fake_run_task)
    user = UserContext(user_id=_OWNER, role="admin")
    resp = await pa_router.resume_pending_action(action.pending_id, user=user, tenant=_TENANT)
    assert resp.run_id == "run-new"
    assert seen["task"].context["_pending_approval"] == {"mock.transfer": action.pending_id}
    assert seen["task"].intent == "转一笔钱"


@pytest.mark.asyncio
async def test_resume_endpoint_refuses_non_owner_and_unapproved(_fresh_store, monkeypatch) -> None:
    import pytest as _pytest
    from fastapi import HTTPException

    import forgeflow.api.routers.pending_actions as pa_router
    from forgeflow.rbac.models import UserContext

    action = _fresh_store.create(
        _TENANT,
        run_id="run-pre-1",
        kind=P.CONFIRM_PERMISSION,
        payload={"tool": "mock.transfer", "owner": _OWNER},
    )
    _fresh_store.resolve(_TENANT, action.pending_id, decision="approve", actor=_OWNER)
    other = UserContext(user_id=_OTHER, role="admin")
    with _pytest.raises(HTTPException) as exc:
        await pa_router.resume_pending_action(action.pending_id, user=other, tenant=_TENANT)
    assert exc.value.status_code == 403

    waiting = _fresh_store.create(
        _TENANT,
        run_id="run-pre-1",
        kind=P.CONFIRM_PERMISSION,
        payload={"tool": "mock.transfer", "owner": _OWNER},
    )
    owner = UserContext(user_id=_OWNER, role="admin")
    with _pytest.raises(HTTPException) as exc:
        await pa_router.resume_pending_action(waiting.pending_id, user=owner, tenant=_TENANT)
    assert exc.value.status_code == 409
