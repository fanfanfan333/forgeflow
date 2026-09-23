"""Hub approval routes — list + decide HITL approvals (docs §4.1).

Mounted at ``/approvals`` alongside the legacy approvals router. Paths are
disjoint (``""`` and ``/{id}/decision`` vs the legacy ``/pending`` /
``/{token}/approve``) so the existing endpoints and their tests are untouched.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from forgeflow.api.dependencies import get_current_user
from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.hub_schemas import (
    ApprovalDecisionRequest,
    ApprovalListResponse,
    ApprovalResponse,
)
from forgeflow.repositories import get_policy_repository
from forgeflow.repositories.base import utcnow
from forgeflow.rbac.models import UserContext

logger = logging.getLogger(__name__)
router = APIRouter()


def _approval_response(record) -> ApprovalResponse:
    return ApprovalResponse(**record.to_dict())


@router.get("", response_model=ApprovalListResponse)
async def list_approvals(
    status: str | None = Query(None),
    risk_level: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    tenant: str = Depends(resolve_tenant),
):
    repo = get_policy_repository()
    rows = await repo.list_approvals(
        tenant, status=status, risk_level=risk_level, limit=limit, offset=offset
    )
    return ApprovalListResponse(total=len(rows), items=[_approval_response(a) for a in rows])


@router.post("/{approval_id}/decision", response_model=ApprovalResponse)
async def decide_approval(
    approval_id: str,
    request: ApprovalDecisionRequest,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    if request.decision not in ("approve", "reject"):
        raise HTTPException(status_code=422, detail="decision must be approve or reject")

    repo = get_policy_repository()
    record = await repo.get_approval(tenant, approval_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    if record.status != "pending":
        raise HTTPException(status_code=409, detail="Approval already resolved")

    record.decision = request.decision
    record.status = "approved" if request.decision == "approve" else "rejected"
    record.approver = user.user_id
    record.note = request.note or record.note
    record.resolved_at = utcnow()
    await repo.save_approval(record)
    logger.info("approval decision | id=%s decision=%s by=%s", approval_id, request.decision, user.user_id)
    return _approval_response(record)
