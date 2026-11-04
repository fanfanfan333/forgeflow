"""INC46 T21 — pending-action routes (the HITL resolve surface).

  * ``GET  /pending-actions``                  — list the caller-tenant's actions.
  * ``POST /pending-actions/{pending_id}/resolve`` — record a human decision.

Design boundaries
-----------------
* **Tenant fail-closed (红线 5).** The tenant is resolved first; an unresolved
  tenant is a real ``403`` (never a silent default). An action owned by
  **another** tenant is also ``403`` — the store refuses rather than inventing a
  shadow copy under the caller's tenant.
* **Timeout is fail-closed (红线 21).** An expired action is never an implicit
  "yes": resolving one is refused (``409``) and the run stays in the additive
  ``expired`` run status.
* **Idempotent resolve.** Resolving an already-resolved action returns the first
  result unchanged — no double count.
* **unmeasured ⇒ None (红线 4).** ``payload`` / ``resolution`` stay ``None``/``{}``
  until a human supplies them; nothing is coerced to ``0`` / ``""``.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from forgeflow.api.dependencies import get_current_user
from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.hub_schemas import RunHandleResponse
from forgeflow.hitl.pending import (
    CONFIRM_PERMISSION,
    RESOLVED,
    CrossTenantPendingAction,
    PendingAction,
    PendingActionExpired,
    PendingActionInvalidDecision,
    PendingActionNotFound,
    PendingActionNotWaiting,
    PendingActionOwnerMismatch,
    get_pending_store,
)
from forgeflow.rbac.models import UserContext
from forgeflow.runtime.dispatcher import load_run_header
from forgeflow.runtime.orchestrator import RequestContext, TaskCreate, run_task
from forgeflow.skills.errors import GovernanceError
from forgeflow.skills.tenant_scope import require_tenant

logger = logging.getLogger(__name__)
router = APIRouter()


class ResolveRequest(BaseModel):
    """A human's decision on a waiting action."""

    decision: str = Field(
        default="approve",
        description="approve|reject|edit|respond",
    )
    actor: str | None = None
    note: str | None = None
    edited_args: dict | None = None
    response_text: str | None = None


class PendingActionView(BaseModel):
    """The wire shape of a :class:`PendingAction` (dates as ISO-8601 strings)."""

    pending_id: str
    run_id: str
    kind: str
    payload: dict
    status: str
    expires_at: str | None = None
    resolution: str | None = None
    resolved_by: str | None = None
    run_status: str
    run_continues: bool


def _tenant_or_403(tenant: str | None) -> str:
    """Fail closed on an unresolved tenant (红线 5)."""
    try:
        return require_tenant(tenant)
    except GovernanceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


def _view(action: PendingAction) -> PendingActionView:
    """Project a :class:`PendingAction` onto the wire model."""
    return PendingActionView(
        pending_id=action.pending_id,
        run_id=action.run_id,
        kind=action.kind,
        payload=dict(action.payload or {}),
        status=action.status,
        expires_at=action.expires_at.isoformat() if action.expires_at else None,
        resolution=action.resolution,
        resolved_by=action.resolved_by,
        run_status=action.run_status,
        run_continues=action.run_continues,
    )


@router.get("", response_model=list[PendingActionView])
async def list_pending_actions(
    run_id: str | None = Query(None),
    status: str | None = Query(None),
    tenant: str = Depends(resolve_tenant),
) -> list[PendingActionView]:
    """List the caller-tenant's pending actions (newest first)."""
    tenant_id = _tenant_or_403(tenant)
    store = get_pending_store()
    return [_view(a) for a in await asyncio.to_thread(store.list, tenant_id, run_id=run_id, status=status)]


@router.post("/{pending_id}/resolve", response_model=PendingActionView)
async def resolve_pending_action(
    pending_id: str,
    payload: ResolveRequest,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
) -> PendingActionView:
    """Record a human decision on a waiting action.

    Returns ``409`` for an expired or otherwise-terminal action (fail closed),
    ``403`` for a cross-tenant action or a non-owner resolver, and ``404``
    for an unknown id.
    """
    tenant_id = _tenant_or_403(tenant)
    store = get_pending_store()
    try:
        action = await asyncio.to_thread(store.resolve,
            tenant_id, pending_id, decision=payload.decision, actor=user.user_id,
            edited_args=payload.edited_args, response_text=payload.response_text,
        )
    except CrossTenantPendingAction as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except PendingActionOwnerMismatch as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except PendingActionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PendingActionExpired as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PendingActionNotWaiting as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PendingActionInvalidDecision as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    logger.info(
        "pending action resolved | tenant=%s pending=%s run=%s decision=%s",
        tenant_id, action.pending_id, action.run_id, action.resolution,
    )
    return _view(action)


@router.post("/{pending_id}/resume", response_model=RunHandleResponse)
async def resume_pending_action(
    pending_id: str,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
) -> RunHandleResponse:
    """Resume a run after its pre-execution approval was granted.

    Phase 4 B-track: mirrors the code-plane approve flow. The action
    must be a RESOLVED-approved ``confirm_permission`` grant (else
    409) and the resumer must be its owner (else 403). A new run
    re-declares the original intent plus a ``_pending_approval``
    mapping, so the gated tool call is injected with the grant and
    the executor lets it through exactly once (success consumes it).
    """
    tenant_id = _tenant_or_403(tenant)
    store = get_pending_store()
    action = await asyncio.to_thread(store.get, tenant_id, pending_id)
    if action is None:
        raise HTTPException(status_code=404, detail="pending action not found")
    payload = dict(action.payload or {})
    if payload.get("owner") and payload["owner"] != user.user_id:
        raise HTTPException(status_code=403, detail="only the action owner may resume it")
    if action.kind != CONFIRM_PERMISSION or action.status != RESOLVED:
        raise HTTPException(status_code=409, detail="pending action is not a resolved pre-execution grant")
    tool = str(payload.get("tool") or "")
    if not tool:
        raise HTTPException(status_code=409, detail="approved action carries no tool")
    if action.resolution not in ("approve", "edit", "respond"):
        raise HTTPException(status_code=409, detail="pending action is not an approved pre-execution grant")
    record = await load_run_header(tenant_id, action.run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="original run not found")
    # One resume, for approve, edit, and respond. The claim is the
    # gate: a lost race or a second call is 409 and starts no run.
    claimed = await asyncio.to_thread(
        store.claim_grant, tenant_id, pending_id, run_id=action.run_id
    )
    if not claimed:
        raise HTTPException(status_code=409, detail="批准已使用过，不能再次恢复")
    ctx = RequestContext(tenant_id=tenant_id, user_id=user.user_id, role=user.role)
    if action.resolution == "respond":
        # Phase 9 A4 — a human answer REPLACES execution: the resumed
        # run carries the answer as a synthetic ok record (the loop
        # seeds it from the task attribute) and the tool never runs.
        task = TaskCreate(
            intent=record.intent,
            title=record.intent[:60],
            workflow_type=str(getattr(record, "workflow_type", "generic") or "generic"),
            context=dict(getattr(record, "declared_inputs", None) or {}),
        )
        task._responded_tool = {
            "tool": tool,
            "response_text": payload.get("response_text") or "",
            "args_hash": payload.get("args_hash"),
        }
        handle = await run_task(task, ctx)
        logger.info(
            "pending action resumed by human response | tenant=%s pending=%s run=%s resume=%s by=%s",
            tenant_id, pending_id, action.run_id, handle.run_id, user.user_id,
        )
        return RunHandleResponse(**handle.to_dict())
    context = dict(getattr(record, "declared_inputs", None) or {})
    context["_pending_approval"] = {tool: pending_id}
    if action.resolution == "edit" and isinstance(payload.get("args"), dict):
        # Phase 9 A4 — edited args are platform-forced at execution.
        context["_pending_approval_args"] = {tool: dict(payload["args"])}
    task = TaskCreate(
        intent=record.intent,
        title=record.intent[:60],
        workflow_type=str(getattr(record, "workflow_type", "generic") or "generic"),
        context=context,
    )
    handle = await run_task(task, ctx)
    logger.info(
        "pending action resumed | tenant=%s pending=%s run=%s resume=%s by=%s",
        tenant_id, pending_id, action.run_id, handle.run_id, user.user_id,
    )
    return RunHandleResponse(**handle.to_dict())
