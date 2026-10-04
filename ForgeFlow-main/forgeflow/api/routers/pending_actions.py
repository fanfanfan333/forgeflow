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

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.hitl.pending import (
    CrossTenantPendingAction,
    PendingAction,
    PendingActionExpired,
    PendingActionNotFound,
    PendingActionNotWaiting,
    get_pending_store,
)
from forgeflow.skills.errors import GovernanceError
from forgeflow.skills.tenant_scope import require_tenant

logger = logging.getLogger(__name__)
router = APIRouter()


class ResolveRequest(BaseModel):
    """A human's decision on a waiting action."""

    decision: str = Field(
        default="approve",
        description="approve|reject|answer|permission",
    )
    actor: str | None = None
    note: str | None = None


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
    return [_view(a) for a in store.list(tenant_id, run_id=run_id, status=status)]


@router.post("/{pending_id}/resolve", response_model=PendingActionView)
async def resolve_pending_action(
    pending_id: str,
    payload: ResolveRequest,
    tenant: str = Depends(resolve_tenant),
) -> PendingActionView:
    """Record a human decision on a waiting action.

    Returns ``409`` for an expired or otherwise-terminal action (fail closed),
    ``403`` for a cross-tenant action, and ``404`` for an unknown id.
    """
    tenant_id = _tenant_or_403(tenant)
    store = get_pending_store()
    try:
        action = store.resolve(
            tenant_id, pending_id, decision=payload.decision, actor=payload.actor
        )
    except CrossTenantPendingAction as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except PendingActionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PendingActionExpired as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PendingActionNotWaiting as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    logger.info(
        "pending action resolved | tenant=%s pending=%s run=%s decision=%s",
        tenant_id, action.pending_id, action.run_id, action.resolution,
    )
    return _view(action)
