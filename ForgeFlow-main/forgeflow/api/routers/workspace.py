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
from forgeflow.governance.policy_engine import evaluate_task_entry
from forgeflow.rbac.models import UserContext
from forgeflow.runtime.attachments import (
    AttachmentTooLargeError,
    prepare_attachments,
)
from forgeflow.runtime.dispatcher import get_run_dispatcher
from forgeflow.runtime.orchestrator import (
    RequestContext,
    TaskCreate,
    _is_code_task,
    get_run_store,
)
from forgeflow.workspace.store import get_workspace_store

logger = logging.getLogger(__name__)
router = APIRouter()


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

    # A real degrade (``pause_noncritical``) sheds new non-critical runs — the
    # same guard the synchronous route uses.
    _admission_guard(request.workflow_type)

    try:
        prepared = await prepare_attachments(request.attachments, intent=request.intent)
    except AttachmentTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc

    ctx = RequestContext(tenant_id=tenant, user_id=user.user_id, role=user.role)
    task = TaskCreate(
        intent=prepared.intent,
        title=request.title or request.intent[:60],
        workflow_type=request.workflow_type,
        context={**request.context, **prepared.context},
    )

    # INC27 — the code-plane gate is checked BEFORE dispatch so a role that may
    # start a run but may not drive the code agent is refused with no run record.
    from forgeflow.runtime.gate import check_code_permission, describe_code_denial

    if _is_code_task(task, ctx) and not check_code_permission(user.role, "code.execute"):
        raise HTTPException(
            status_code=403, detail=describe_code_denial(user.role, "code.execute")
        )

    handle = await get_run_dispatcher().dispatch(
        task,
        ctx,
        session_id=request.session_id,
        parent_run_id=request.parent_run_id,
    )
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
):
    """List the tenant's conversations (newest activity first)."""
    groups = await get_workspace_store().list_sessions(tenant, limit=limit)
    items = [SessionSummaryResponse(**group) for group in groups]
    return SessionListResponse(total=len(items), items=items)


@router.get("/sessions/{session_id}", response_model=WorkspaceSessionDetailResponse)
async def get_session(session_id: str, tenant: str = Depends(resolve_tenant)):
    """Fetch one conversation's run headers (oldest first)."""
    runs = await get_workspace_store().list_session_runs(tenant, session_id)
    if not runs:
        raise HTTPException(status_code=404, detail="Session not found")
    earliest = runs[0]
    return WorkspaceSessionDetailResponse(
        session_id=session_id,
        title=earliest.title or earliest.intent[:60],
        runs=[r.to_dict() for r in runs],
    )


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, tenant: str = Depends(resolve_tenant)):
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
    deleted = await get_workspace_store().soft_delete_session(tenant, session_id)
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
    discarded = get_run_store().discard_session(session_id)
    logger.info(
        "workspace session soft-deleted | session=%s runs=%d discarded=%d",
        session_id,
        deleted,
        discarded,
    )
    return {"session_id": session_id, "deleted": deleted}
