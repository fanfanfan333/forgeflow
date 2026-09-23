"""Policy routes — list/create policies and evaluate a decision (docs §4.1)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.hub_schemas import (
    EvalDecisionResponse,
    EvalRequest,
    PolicyCreateRequest,
    PolicyListResponse,
    PolicyResponse,
)
from forgeflow.governance.models import POLICY_EFFECTS, PolicyRecord
from forgeflow.governance.policy_engine import PolicyEngine
from forgeflow.repositories import get_policy_repository

logger = logging.getLogger(__name__)
router = APIRouter()


def _policy_response(policy: PolicyRecord) -> PolicyResponse:
    return PolicyResponse(**policy.to_dict())


@router.get("", response_model=PolicyListResponse)
async def list_policies(
    subject: str | None = Query(None),
    resource: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    tenant: str = Depends(resolve_tenant),
):
    repo = get_policy_repository()
    rows = await repo.list_policies(
        tenant, subject=subject, resource=resource, limit=limit, offset=offset
    )
    return PolicyListResponse(total=len(rows), items=[_policy_response(p) for p in rows])


@router.post("", response_model=PolicyResponse)
async def create_policy(
    request: PolicyCreateRequest,
    tenant: str = Depends(resolve_tenant),
):
    if request.effect not in POLICY_EFFECTS:
        raise HTTPException(
            status_code=422, detail=f"effect must be one of {POLICY_EFFECTS}"
        )
    policy = PolicyRecord(
        tenant_id=tenant,
        subject=request.subject,
        resource=request.resource,
        action=request.action,
        condition=request.condition,
        effect=request.effect,
        description=request.description,
    )
    await get_policy_repository().save_policy(policy)
    return _policy_response(policy)


@router.post("/evaluate", response_model=EvalDecisionResponse)
async def evaluate_policy(
    request: EvalRequest,
    tenant: str = Depends(resolve_tenant),
):
    """Run the PolicyEngine for a (subject, resource, action) triple."""
    engine = PolicyEngine()
    decision = await engine.evaluate(
        subject=request.subject,
        resource=request.resource,
        action=request.action,
        context=request.context,
        tenant_id=tenant,
    )
    return EvalDecisionResponse(**decision.to_dict())
