"""Workspace BFF routes — the async dispatch + session surface (INC32 ADR-01/06).

Lives in the **same** FastAPI app as everything else (ADR-06): reusing the
existing RBAC / tenant / JWT / audit / rate-limit middleware and — crucially —
sharing the in-process ``RunEventBus`` + ``MemoryRunStore``, which a separate
process could not reach (the SSE stream and the running entity must live in one
address space).

Endpoints:
  * ``POST /workspace/tasks``           — async dispatch; returns the handle at once
  * ``GET  /workspace/sessions``        — group the tenant's runs into conversations
  * ``GET  /workspace/sessions/{sid}``  — one conversation's run headers
  * ``DELETE /workspace/sessions/{sid}`` — soft-delete a conversation (INC42 / Q4=A / Q5=B)

``POST /tasks`` (synchronous) is **unchanged** — the async lifecycle is exposed
only here (ADR-01). RBAC is gated by ``ROUTE_PERMISSION_MAP``'s ``/workspace``
entries (both added by INC32; the ``DELETE`` entry added by INC42; no existing
entry touched).
"""

from __future__ import annotations

import logging
import base64
import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException, Query

from forgeflow.api.dependencies import get_current_user
from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.hub_schemas import (
    RunHandleResponse,
    SessionListResponse,
    SessionSummaryResponse,
    WorkspaceSessionDetailResponse,
    WorkspaceTaskCreateRequest,
)
from forgeflow.api.routers.tasks import _admission_guard
from forgeflow.config import get_settings
from forgeflow.governance.policy_engine import evaluate_task_entry
from forgeflow.rbac.models import UserContext
from forgeflow.runtime.attachments import (
    AttachmentTooLargeError,
    prepare_attachments,
)
from forgeflow.runtime.dispatcher import RunCapacityError, RunIdempotencyConflict, RunStorageError, get_run_dispatcher
from forgeflow.runtime.orchestrator import (
    RequestContext,
    TaskCreate,
    _is_code_task,
    get_run_store,
)
from forgeflow.workspace.store import get_workspace_store

logger = logging.getLogger(__name__)
router = APIRouter()


def _request_digest(request: WorkspaceTaskCreateRequest) -> str:
    payload = request.model_dump(exclude={"request_key"})
    limit = int(get_settings().multimodal_max_bytes)
    for attachment in payload.get("attachments", []):
        encoded = attachment.get("data_base64") or ""
        if len(encoded) > 4 * ((limit + 2) // 3):
            raise AttachmentTooLargeError("附件超过大小限制")
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > limit:
            raise AttachmentTooLargeError("附件超过大小限制")
        attachment["data_base64"] = hashlib.sha256(raw).hexdigest()
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@router.get("/identity")
async def workspace_identity(user: UserContext = Depends(get_current_user),
                             tenant: str = Depends(resolve_tenant)):
    return {"tenant_id": tenant, "user_id": user.user_id}


@router.get("/requests/{request_key}", response_model=RunHandleResponse)
async def get_request_run(request_key: str, user: UserContext = Depends(get_current_user),
                          tenant: str = Depends(resolve_tenant)):
    from forgeflow.runtime.dispatcher import load_run_header, request_run_id

    run_id = request_run_id(tenant, user.user_id, request_key)
    record = await load_run_header(tenant, run_id)
    if record is None or record.actor_user_id != user.user_id:
        raise HTTPException(status_code=404, detail="Run not found")
    return RunHandleResponse(run_id=run_id, thread_id=record.thread_id or run_id,
                             status=record.status, session_id=record.session_id,
                             parent_run_id=record.parent_run_id)


@router.post("/tasks", response_model=RunHandleResponse)
async def create_workspace_task(
    request: WorkspaceTaskCreateRequest,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """Create a task and **dispatch it asynchronously**; return the handle now.

    Same admission + risk + attachment + code-gate checks as the synchronous
    ``POST /tasks`` (INC32 must not open a weaker door), but the run itself
    executes in the background so the caller can address it (SSE / Stop /
    artifacts) immediately.
    """
    if "handoff_results" in request.context:
        from forgeflow.runtime.handoff import validate_handoff_results
        try:
            validate_handoff_results(request.context["handoff_results"])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if request.session_id:
        previous = await get_workspace_store().list_session_runs(tenant, request.session_id)
        if not previous or any(r.actor_user_id != user.user_id for r in previous):
            raise HTTPException(status_code=404, detail="Session not found")

    # A parent relationship is an object reference, not just a label: attaching
    # to another user's run would disclose that it exists and let a new task
    # inherit its context. Resolve it through the same owner-scoped reader as
    # GET /runs/{id}, and return 404 so the id stays unconfirmable.
    parent_run_id = request.parent_run_id or str(
        request.context.get("parent_run_id") or ""
    )
    if parent_run_id:
        from forgeflow.runtime.dispatcher import load_run_header

        parent = await load_run_header(tenant, parent_run_id)
        if (
            parent is None
            or parent.tenant_id != tenant
            or parent.actor_user_id != user.user_id
        ):
            raise HTTPException(status_code=404, detail="Parent run not found")
    # Cross-project handoff source: same owner-scoped check as a parent.
    # This also hydrates the source into the in-process run store, so
    # the orchestrator's server-side derivation (``_codeplane_args``)
    # can find it even after a restart. A foreign / missing source is
    # a generic 404, never a disclosure of whose run it was.
    handoff_from = request.context.get("handoff_from_run_id")
    if handoff_from:
        if not isinstance(handoff_from, str) or not handoff_from.strip():
            raise HTTPException(status_code=422, detail="handoff_from_run_id 无效")
        from forgeflow.runtime.dispatcher import load_run_header

        source = await load_run_header(tenant, handoff_from.strip())
        if (
            source is None
            or source.tenant_id != tenant
            or source.actor_user_id != user.user_id
        ):
            raise HTTPException(status_code=404, detail="Handoff source run not found")
    decision = await evaluate_task_entry(
        user.user_id, user.role, request.intent, tenant_id=tenant
    )
    if not decision.allowed:
        raise HTTPException(status_code=403, detail=decision.reason)
    if decision.requires_approval:
        raise HTTPException(
            status_code=403,
            detail=f"high-risk task requires human approval: {decision.reason}",
        )

    try:
        raw_digest = _request_digest(request) if request.request_key else None
    except AttachmentTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="附件内容无效") from exc

    ctx = RequestContext(tenant_id=tenant, user_id=user.user_id, role=user.role)
    if request.request_key:
        from forgeflow.runtime.dispatcher import request_run_id
        from forgeflow.runtime.input_scope import InputReferenceError, prepare_input_scope
        from forgeflow.runtime.gate import check_code_permission, describe_code_denial

        raw_task = TaskCreate(intent=request.intent, title=request.title or request.intent[:60],
                              workflow_type=request.workflow_type, context=dict(request.context))
        try:
            await prepare_input_scope(raw_task, ctx)
        except InputReferenceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        if _is_code_task(raw_task, ctx) and not check_code_permission(user.role, "code.execute"):
            raise HTTPException(status_code=403, detail=describe_code_denial(user.role, "code.execute"))
        if request.session_id or request.parent_run_id:
            raise HTTPException(status_code=409, detail="幂等任务不支持指定会话或父任务")
        run_id = request_run_id(tenant, user.user_id, request.request_key)
        try:
            prior = await get_workspace_store().get(tenant, run_id)
        except Exception as exc:
            raise HTTPException(status_code=503, detail="workspace storage unavailable") from exc
        if prior is not None:
            if prior.actor_user_id != user.user_id or prior.request_digest != raw_digest:
                raise HTTPException(status_code=409, detail="幂等键已用于不同任务或历史请求无法核验")
            raw_task.intent = prior.intent
            if _is_code_task(raw_task, ctx) and not check_code_permission(user.role, "code.execute"):
                raise HTTPException(status_code=403, detail=describe_code_denial(user.role, "code.execute"))
            return RunHandleResponse(run_id=run_id, thread_id=run_id, status=prior.status,
                                     session_id=prior.session_id, parent_run_id=prior.parent_run_id)

    # Replays remain available when admission of new work is paused.
    _admission_guard(request.workflow_type)

    try:
        prepared = await prepare_attachments(request.attachments, intent=request.intent)
    except AttachmentTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc

    task = TaskCreate(
        intent=prepared.intent,
        title=request.title or request.intent[:60],
        workflow_type=request.workflow_type,
        context={**request.context, **prepared.context},
    )

    # Parent relationships supplied by the workspace API are input references too.
    task.context["parent_run_id"] = request.parent_run_id or task.context.get("parent_run_id", "")
    from forgeflow.runtime.input_scope import InputReferenceError, prepare_input_scope

    try:
        await prepare_input_scope(task, ctx)
    except InputReferenceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    # INC27 — the code-plane gate is checked BEFORE dispatch so a role that may
    # start a run but may not drive the code agent is refused with no run record.
    from forgeflow.runtime.gate import check_code_permission, describe_code_denial

    if _is_code_task(task, ctx) and not check_code_permission(user.role, "code.execute"):
        raise HTTPException(
            status_code=403, detail=describe_code_denial(user.role, "code.execute")
        )

    try:
        handle = await get_run_dispatcher().dispatch(
            task, ctx, session_id=request.session_id, parent_run_id=request.parent_run_id,
            **({"request_key": request.request_key, "request_digest": raw_digest} if request.request_key else {}),
        )
    except RunCapacityError as exc:
        raise HTTPException(
            status_code=503, detail=str(exc), headers={"Retry-After": "5"}
        ) from exc
    except RunStorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RunIdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    logger.info(
        "workspace dispatch | run=%s session=%s parent=%s by=%s",
        handle.run_id,
        handle.session_id,
        handle.parent_run_id,
        user.user_id,
    )
    return RunHandleResponse(**handle.to_dict())


@router.get("/sessions", response_model=SessionListResponse)
async def list_sessions(
    limit: int = Query(20, ge=1, le=100),
    tenant: str = Depends(resolve_tenant),
    user: UserContext = Depends(get_current_user),
):
    """List the tenant's conversations (newest activity first)."""
    groups = await get_workspace_store().list_sessions(
        tenant, limit=limit, actor_user_id=user.user_id
    )
    items = [SessionSummaryResponse(**group) for group in groups]
    return SessionListResponse(total=len(items), items=items)


@router.get("/sessions/{session_id}", response_model=WorkspaceSessionDetailResponse)
async def get_session(session_id: str, tenant: str = Depends(resolve_tenant),
                      user: UserContext = Depends(get_current_user)):
    """Fetch one conversation's run headers (oldest first)."""
    runs = await get_workspace_store().list_session_runs(tenant, session_id)
    if not runs or any(r.actor_user_id != user.user_id for r in runs):
        raise HTTPException(status_code=404, detail="Session not found")
    earliest = runs[0]
    return WorkspaceSessionDetailResponse(
        session_id=session_id,
        title=earliest.title or earliest.intent[:60],
        runs=[r.to_dict() for r in runs],
    )


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, tenant: str = Depends(resolve_tenant),
                         user: UserContext = Depends(get_current_user)):
    """Soft-delete one conversation and all of its runs (INC42 / Q4=A + Q5=B).

    Deleting history is a **soft delete**: every run of ``session_id`` for this
    tenant gets ``deleted_at = now`` and every read path then filters it out —
    the rows are **kept**, so the audit chain survives. ``tenant_id`` is a
    mandatory predicate inside ``soft_delete_session`` (a cross-tenant call can
    never touch another tenant's session).

    HTTP semantics are honest:
      * an existing session with ≥1 live run → **200** ``{session_id, deleted}``;
      * an unknown / already-deleted / cross-tenant session → **404**
        (``deleted == 0`` — honest, never a fabricated success);
      * a role without ``execute:workflows`` → **403** at the RBAC middleware
        (``("DELETE", "/workspace")`` → ``("execute", "workflows")``).

    RBAC is aligned with ``canExecute`` on the home page (Q6=A): only an
    identity that may start a task may delete one.
    """
    from forgeflow.config import get_settings
    owned = await get_workspace_store().list_session_runs(tenant, session_id)
    if not owned or any(r.actor_user_id != user.user_id for r in owned):
        raise HTTPException(status_code=404, detail="Session not found")
    records = owned if get_settings().openviking_sync_enabled else []
    deleted = await get_workspace_store().soft_delete_session(tenant, session_id, actor_user_id=user.user_id)
    if deleted == 0:
        raise HTTPException(status_code=404, detail="Session not found")
    # INC42 (P1 — FIX-1) — invalidate the **process-local** run cache so the soft
    # delete is immediately visible to ``GET /runs`` / ``GET /runs/{id}``.
    # ``soft_delete_session`` only flips ``deleted_at`` in ``workspace_runs``;
    # without this, a run this process created (``MemoryRunStore``) would still be
    # served and ``history.ts::mergeHistoryRuns`` lists the volatile ``GET /runs``
    # items first ⇒ the deleted session would "resurrect" on the home page. Only
    # the success branch (``deleted > 0``) invalidates; the 404 branch above must
    # never touch the store. ``workspace.py`` already imports from ``orchestrator``
    # at module scope (no new cycle — ``orchestrator`` imports ``workspace`` only
    # lazily inside functions).
    discarded = get_run_store().discard_session(session_id, tenant_id=tenant, actor_user_id=user.user_id)
    if records:
        from forgeflow.context.viking_history import revoke_history
        await revoke_history(tenant, records)
    logger.info(
        "workspace session soft-deleted | session=%s runs=%d discarded=%d",
        session_id,
        deleted,
        discarded,
    )
    return {"session_id": session_id, "deleted": deleted}
