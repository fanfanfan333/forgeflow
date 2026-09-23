"""Marketplace API — workflow templates **and** skill-level listings (INC2-12)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from forgeflow.api.dependencies import get_current_user
from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.marketplace.registry import get_registry
from forgeflow.rbac.models import UserContext
from forgeflow.rbac.policies import ROLE_PERMISSIONS
from forgeflow.skills.marketplace_bridge import (
    MarketplaceError,
    install_listing,
    publish_listing,
    rate_listing,
    search_listings,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# --- skill-level marketplace (INC2-12) --------------------------------------


class SkillPublishRequest(BaseModel):
    """Publish request. ``shared`` defaults to FALSE (design verdict §7.3)."""

    skill_id: str = Field(..., description="Skill to publish")
    shared: bool = Field(False, description="Visible to other tenants")
    description: str = Field("", description="Listing blurb")
    version: str | None = Field(None, description="Override the version label")


class SkillRateRequest(BaseModel):
    score: int = Field(..., ge=1, le=5, description="Rating 1..5")
    comment: str = Field("", description="Optional comment")


def _permissions_for(role: str) -> list[str]:
    return sorted(ROLE_PERMISSIONS.get(role, set()))


@router.get("/skills")
async def list_skill_listings(
    q: str | None = Query(None, description="Substring match on name/description"),
    domain: str | None = Query(None, description="Filter by skill domain"),
    cross_tenant: bool = Query(False, description="Also include shared listings"),
    limit: int = Query(20, ge=1, le=100),
    tenant: str = Depends(resolve_tenant),
) -> dict:
    """Search skill listings — own tenant always, other tenants only if shared."""
    listings = await search_listings(
        tenant, q=q, domain=domain, include_cross_tenant=cross_tenant, limit=limit
    )
    return {"total": len(listings), "items": [l.to_dict() for l in listings]}


@router.post("/skills/publish")
async def publish_skill_listing(
    request: SkillPublishRequest,
    tenant: str = Depends(resolve_tenant),
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Publish a skill. Pre-checked by DLP + the trust baseline (INC2-13).

    A rejection is a 403 with the concrete reason; ``AuditMiddleware`` records
    every response (including these 403s) so the attempt stays auditable.
    """
    try:
        listing = await publish_listing(
            tenant,
            request.skill_id,
            actor=user.user_id,
            actor_permissions=_permissions_for(user.role),
            shared=request.shared,
            description=request.description,
            version=request.version,
        )
    except MarketplaceError as exc:
        logger.warning(
            "marketplace.publish blocked user=%s skill=%s shared=%s reason=%s",
            user.user_id,
            request.skill_id,
            request.shared,
            exc.detail,
        )
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {"published": True, "listing": listing.to_dict()}


@router.post("/skills/{listing_id}/install")
async def install_skill_listing(
    listing_id: str,
    tenant: str = Depends(resolve_tenant),
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Install a listing into the caller's tenant."""
    try:
        return await install_listing(tenant, listing_id, actor=user.user_id)
    except MarketplaceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.post("/skills/{listing_id}/rate")
async def rate_skill_listing(
    listing_id: str,
    request: SkillRateRequest,
    tenant: str = Depends(resolve_tenant),
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Rate a listing; the running average is recomputed on the listing row."""
    try:
        listing = await rate_listing(
            tenant,
            listing_id,
            request.score,
            user_id=user.user_id,
            comment=request.comment,
        )
    except MarketplaceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {"rated": True, "listing": listing.to_dict()}


@router.get("/templates")
async def list_templates(
    domain: str | None = Query(None, description="Filter by workflow domain"),
    tag: str | None = Query(None, description="Filter by tag substring (case-insensitive)"),
) -> dict:
    """List discovered workflow templates."""
    registry = get_registry()
    manifests = registry.list_all()

    if domain:
        manifests = [m for m in manifests if m.domain == domain]
    if tag:
        needle = tag.lower()
        manifests = [m for m in manifests if any(needle in t.lower() for t in m.tags)]

    return {
        "total": len(manifests),
        "templates": [m.to_dict() for m in manifests],
    }


@router.get("/templates/{name}")
async def get_template(name: str) -> dict:
    """Fetch a single template manifest by name."""
    m = get_registry().get(name)
    if m is None:
        raise HTTPException(status_code=404, detail=f"template '{name}' not found")
    return m.to_dict()


@router.post("/templates/refresh")
async def refresh() -> dict:
    """Re-scan the template search paths. Useful after dropping a manifest into
    templates/community/ at runtime without an API restart."""
    registry = get_registry()
    registry.discover(refresh=True)
    return {"refreshed": True, "total": len(registry.list_all())}
