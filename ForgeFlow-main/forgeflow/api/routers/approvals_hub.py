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
from forgeflow.rbac.models import UserContext
from forgeflow.repositories import get_policy_repository
from forgeflow.repositories.base import utcnow

logger = logging.getLogger(__name__)
router = APIRouter()


async def _visible_approval(record, tenant: str, user: UserContext) -> bool:
    from forgeflow.api.run_access import REVIEW_ROLES
    from forgeflow.mcp.approvals import APPROVAL_KEY_PREFIX
    from forgeflow.runtime.dispatcher import load_run_header

    # An MCP write is the one approval whose whole point is that *someone else*
    # decides: only the run owner could see it before, which made "per-call human
    # approval" mean "the requester clicks approve on their own agent". The
    # reviewer seats share the pool (same predicate as run-detail review), while
    # the requester keeps read access to their own request.
    if str(getattr(record, "requested_action", "") or "").startswith(APPROVAL_KEY_PREFIX):
        if not record.run_id:
            return True
        run = await load_run_header(tenant, record.run_id)
        if run is not None and run.actor_user_id == user.user_id:
            return True
        return str(getattr(user, "role", "") or "") in REVIEW_ROLES

    if not record.run_id:
        return True
    run = await load_run_header(tenant, record.run_id)
    return run is not None and run.actor_user_id == user.user_id


def _approval_response(record) -> ApprovalResponse:
    return ApprovalResponse(**record.to_dict())


def self_approval_problem(record, user: UserContext, *, allowed: bool = False) -> str:
    """Why ``user`` may not decide this approval; ``""`` means it is allowed.

    Machine-raised approvals (``mcp.write:*``) exist so that a *second* person
    decides that an agent may write. Letting the requester decide their own
    request turns the gate into a formality the agent can satisfy itself, so it
    is refused by default. ``mcp_write_allow_self_approval`` is the explicit,
    documented escape hatch for single-operator deployments — an informed choice,
    not a silent default.
    """
    from forgeflow.mcp.approvals import APPROVAL_KEY_PREFIX

    if allowed:
        return ""
    action = str(getattr(record, "requested_action", "") or "")
    if not action.startswith(APPROVAL_KEY_PREFIX):
        return ""
    requester = str(getattr(record, "requester", "") or "")
    decider = str(getattr(user, "user_id", "") or "")
    if requester and requester == decider:
        return "an MCP write must be approved by someone other than the requester"
    return ""


def _refuse_self_approval(record, user: UserContext) -> None:
    from forgeflow.config import get_settings

    problem = self_approval_problem(
        record, user, allowed=bool(get_settings().mcp_write_allow_self_approval)
    )
    if problem:
        raise HTTPException(status_code=403, detail=problem)


@router.get("", response_model=ApprovalListResponse)
async def list_approvals(
    status: str | None = Query(None),
    risk_level: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    tenant: str = Depends(resolve_tenant),
    user: UserContext = Depends(get_current_user),
):
    repo = get_policy_repository()
    rows = await repo.list_approvals(
        tenant, status=status, risk_level=risk_level, limit=limit, offset=offset
    )
    rows = [row for row in rows if await _visible_approval(row, tenant, user)]
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
    if not await _visible_approval(record, tenant, user):
        raise HTTPException(status_code=404, detail="Approval not found")
    if record.status != "pending":
        raise HTTPException(status_code=409, detail="Approval already resolved")
    _refuse_self_approval(record, user)

    record.decision = request.decision
    record.status = "approved" if request.decision == "approve" else "rejected"
    record.approver = user.user_id
    record.note = request.note or record.note
    record.resolved_at = utcnow()
    await repo.save_approval(record)
    logger.info(
        "approval decision | id=%s decision=%s by=%s", approval_id, request.decision, user.user_id
    )
    return _approval_response(record)
