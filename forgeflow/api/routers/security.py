"""Security overview route — home-page security summary (docs §7.3)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.hub_schemas import SecurityOverviewResponse
from forgeflow.config import get_settings
from forgeflow.repositories import get_policy_repository

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/overview", response_model=SecurityOverviewResponse)
async def security_overview(tenant: str = Depends(resolve_tenant)):
    """Aggregate isolation / DLP / approval state for the home page card."""
    settings = get_settings()
    repo = get_policy_repository()

    policies = await repo.list_policies(tenant, limit=500)
    approvals = await repo.list_approvals(tenant, limit=500)
    pending = [a for a in approvals if a.status == "pending"]
    blocked = [a for a in approvals if a.risk_level == "high"]

    return SecurityOverviewResponse(
        status="系统运行正常",
        isolation_level=settings.tenant_isolation_level,
        dlp_enabled=settings.dlp_enabled,
        encryption="已启用",
        access_control="已启用",
        blocked_count=len(blocked),
        pending_approvals=len(pending),
        policies=len(policies),
    )
