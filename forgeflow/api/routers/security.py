"""Security overview route — home-page security summary (docs §7.3)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from forgeflow.api.dependencies import get_current_user
from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.hub_schemas import SecurityOverviewResponse
from forgeflow.config import get_settings
from forgeflow.rbac.models import UserContext
from forgeflow.repositories import get_policy_repository

logger = logging.getLogger(__name__)
router = APIRouter()


def _require_admin(user: UserContext) -> None:
    """Admin-only gate for the quarantine review surface.

    The RBAC middleware maps ``/security/quarantine`` to the audit family
    (``read:audit`` is held by manager as well), so the stricter "admin only"
    rule the task book requires is enforced here — the same handler-level
    tightening pattern used by ``POST /eval/golden-sets`` (人工导入仅 admin).
    """
    if user.role != "admin":
        raise HTTPException(
            status_code=403,
            detail="quarantine review requires the admin role",
        )


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


# --------------------------------------------------------------------------- #
# INC46 T33 — experience quarantine review surface (人工复核隔离区).           #
# 命中指令性文本的 Experience 在此等待复核；release 后才重新进入 experiences    #
# （因而才可能被 Miner 读到）。RBAC: 路由映射到 read:audit（manager 亦持有），  #
# 「仅 admin」由本文件的 _require_admin 再收紧一次 —— 与 /eval/golden-sets 的  #
# 做法一致；不新增权限、不放宽任何角色。                                        #
# --------------------------------------------------------------------------- #

@router.get("/quarantine")
async def list_quarantine_entries(
    status: str | None = Query(None, description="quarantined | released"),
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
) -> dict:
    """List the caller-tenant's quarantined experiences (admin only).

    Tenant fail-closed: the store only ever returns the caller's partition, so
    there is no cross-tenant read and no existence leak.
    """
    _require_admin(user)
    from forgeflow.security.quarantine import list_quarantine

    rows = list_quarantine(tenant, status=status)
    return {
        "tenant_id": tenant,
        "total": len(rows),
        "items": [row.to_dict() for row in rows],
    }


@router.post("/quarantine/{entry_id}/release")
async def release_quarantine_entry(
    entry_id: str,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
) -> dict:
    """Human-review release of one quarantined experience (admin only).

    Marks the row ``released`` and re-admits the (already scrubbed) experience
    into ``experiences`` so it may re-enter the miner. Unknown / cross-tenant id
    ⇒ **404** (no existence leak). Idempotent: a second call is a no-op.
    """
    _require_admin(user)
    from forgeflow.security.quarantine import release_quarantine

    released = await release_quarantine(tenant, entry_id, released_by=user.user_id)
    if released is None:
        raise HTTPException(status_code=404, detail="Quarantine entry not found")
    return {"released": True, "entry": released.to_dict()}
