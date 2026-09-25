"""INC2-12 — Skill-level marketplace (publish / search / install / rate).

The template marketplace already exists (``forgeflow.marketplace.registry``);
what was missing is *skills* — user ask #5 (技能共享). Listings live in
``skill_listings`` (migration 010) extended by 011 with ``shared``,
``listed_by``, ``rating``, ``rating_count``; ratings live in ``skill_ratings``.

Two hard rules from design verdict §7.3:

* ``shared`` defaults to ``FALSE`` — publishing is private until explicitly
  flipped, and flipping it cross-tenant requires the explicit
  ``approve:skills`` grant.
* publishing runs the **dual pre-check**: DLP scan + trust baseline
  (INC2-13). Either failure ⇒ ``MarketplaceError(403)`` and the caller writes
  the audit entry. Nothing reaches the table otherwise.

Storage note (the ``NULL UNIQUE`` trap): ``skill_listings`` is keyed by
``UNIQUE (skill_id, version)`` and listings are naturally identified by
``(tenant_id, skill_id)``. ``tenant_id`` is an opaque ``TEXT`` partition key
(``_scope`` → the tenant slug, or ``"global"`` for a missing tenant), so we
never rely on ``ON CONFLICT`` for that pair — we UPDATE first (with
``IS NOT DISTINCT FROM``) and only INSERT when zero rows matched.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from forgeflow.config import get_settings
from forgeflow.repositories.base import new_id, scope_key, utcnow
from forgeflow.skills.trust_baseline import TrustReport, verify_trust_baseline

logger = logging.getLogger(__name__)

__all__ = [
    "MarketplaceError",
    "SkillListing",
    "install_listing",
    "publish_listing",
    "rate_listing",
    "reset_marketplace",
    "search_listings",
]

#: Grant required to publish a listing visible to other tenants (§7.3).
CROSS_TENANT_PERMISSION = "approve:skills"


class MarketplaceError(Exception):
    """Marketplace rejection — carries the HTTP status the router should use."""

    def __init__(self, message: str, status_code: int = 403) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = message


@dataclass
class SkillListing:
    """One marketplace entry for a versioned skill."""

    id: str = field(default_factory=new_id)
    tenant_id: str | None = None
    skill_id: str = ""
    version: str = "0.1.0"
    name: str = ""
    domain: str = ""
    description: str = ""
    spec: dict[str, Any] = field(default_factory=dict)
    shared: bool = False
    listed_by: str | None = None
    rating: float = 0.0
    rating_count: int = 0
    installs: int = 0
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "skill_id": self.skill_id,
            "version": self.version,
            "name": self.name,
            "domain": self.domain,
            "description": self.description,
            "shared": self.shared,
            "listed_by": self.listed_by,
            "rating": round(self.rating, 2),
            "rating_count": self.rating_count,
            "installs": self.installs,
            "created_at": self.created_at.isoformat(),
        }


# Offline (memory backend) store. Keyed by tenant scope like the repositories.
_LISTINGS: dict[str, dict[str, SkillListing]] = {}
_RATINGS: dict[str, list[dict[str, Any]]] = {}


def _scope(tenant_id: str | None) -> str:
    """Partition key for a tenant — the marketplace's ``scope_key``.

    ``skill_listings.tenant_id`` / ``skill_ratings.tenant_id`` are opaque
    ``TEXT`` (migration 013), so the PostgreSQL backend partitions by exactly
    the same key as the in-process store: the tenant slug, or ``"global"`` for a
    missing tenant. There is no UUID coercion, so distinct non-UUID tenants stay
    distinct rather than collapsing onto one shared bucket.
    """
    return scope_key(tenant_id, "global")


def reset_marketplace() -> None:
    """Test helper — drop every in-memory listing/rating."""
    _LISTINGS.clear()
    _RATINGS.clear()


def _affected_rows(status: Any) -> int:
    if not status:
        return 0
    parts = str(status).split()
    if len(parts) < 2 or not parts[0].isalpha():
        return 0
    try:
        return int(parts[-1])
    except ValueError:
        return 0


def _is_postgres() -> bool:
    return get_settings().storage_backend.lower() == "postgres"


def _has_permission(permissions: list[str], permission: str) -> bool:
    grants = set(permissions)
    if "*:*" in grants:
        return True
    if permission in grants:
        return True
    action, _, resource = permission.partition(":")
    return f"{action}:*" in grants


async def _load_skill(tenant_id: str | None, skill_id: str) -> Any | None:
    """Fetch the backing skill so a listing can carry name/domain/spec."""
    try:
        from forgeflow.repositories import get_skill_repository

        repo = get_skill_repository()
        skill = await repo.get_skill(tenant_id, skill_id)
        if skill is not None:
            return skill
    except Exception as exc:  # noqa: BLE001
        logger.debug("marketplace: skill lookup failed: %s", exc)
    return None


def _listing_from_skill(skill: Any, tenant_id: str | None) -> SkillListing:
    return SkillListing(
        tenant_id=tenant_id,
        skill_id=str(getattr(skill, "id", "") or ""),
        version=str(getattr(skill, "current_version", "") or "0.1.0"),
        name=str(getattr(skill, "name", "") or ""),
        domain=str(getattr(skill, "domain", "") or ""),
        description=str(getattr(skill, "description", "") or ""),
    )


async def publish_listing(
    tenant_id: str | None,
    skill_id: str,
    *,
    actor: str = "anonymous",
    actor_permissions: list[str] | None = None,
    shared: bool = False,
    spec: dict[str, Any] | None = None,
    description: str = "",
    version: str | None = None,
) -> SkillListing:
    """Publish a skill to the marketplace.

    Dual pre-check (INC2-12 + INC2-13): DLP, then the trust baseline. Any
    failure raises ``MarketplaceError(403)`` *before* a row is written.

    Raises:
        MarketplaceError(404): skill not found.
        MarketplaceError(403): DLP hit, trust-baseline failure, or the actor
            lacks ``approve:skills`` for a cross-tenant (``shared``) listing.
    """
    permissions = list(actor_permissions or [])
    skill = await _load_skill(tenant_id, skill_id)
    if skill is None:
        raise MarketplaceError(f"skill '{skill_id}' not found", status_code=404)

    listing = _listing_from_skill(skill, tenant_id)
    if description:
        listing.description = description
    if version:
        listing.version = version
    listing.spec = dict(spec or getattr(skill, "spec", {}) or {})
    listing.listed_by = actor

    # --- §7.3: shared is opt-in and gated ---------------------------------
    if shared and not _has_permission(permissions, CROSS_TENANT_PERMISSION):
        raise MarketplaceError(
            f"跨租户共享需要显式权限 {CROSS_TENANT_PERMISSION}；当前发布者不具备"
        )
    listing.shared = bool(shared)

    # --- pre-check ①: DLP --------------------------------------------------
    payload = json.dumps(
        {"spec": listing.spec, "description": listing.description},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    try:
        from forgeflow.governance.dlp import DlpGate

        dlp = DlpGate().scan(payload)
    except Exception as exc:  # noqa: BLE001 — a DLP crash must not mean "clean"
        raise MarketplaceError(f"DLP 扫描失败，按拒绝处理：{exc}") from exc
    if getattr(dlp, "pii_found", False):
        categories = ", ".join(getattr(dlp, "categories", []) or [])
        raise MarketplaceError(f"上架被拒：技能 spec 命中敏感信息（{categories}）")

    # --- pre-check ②: trust baseline (INC2-13) ----------------------------
    report: TrustReport = verify_trust_baseline(listing.spec, permissions)
    if not report.ok:
        raise MarketplaceError(f"上架被拒：{report.reason}")

    return await _save_listing(listing)


async def _save_listing(listing: SkillListing) -> SkillListing:
    """Persist a listing — memory dict, or NULL-safe UPDATE-then-INSERT in PG."""
    if not _is_postgres():
        _LISTINGS.setdefault(_scope(listing.tenant_id), {})[listing.skill_id] = listing
        return listing

    try:
        from forgeflow.database import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            status = await conn.execute(
                """
                UPDATE skill_listings SET
                    version     = $3,
                    description = $4,
                    shared      = $5,
                    listed_by   = $6
                WHERE tenant_id IS NOT DISTINCT FROM $1
                  AND skill_id = $2
                """,
                _scope(listing.tenant_id),
                listing.skill_id,
                listing.version,
                listing.description,
                listing.shared,
                listing.listed_by,
            )
            if _affected_rows(status) == 0:
                await conn.execute(
                    """
                    INSERT INTO skill_listings
                      (id, tenant_id, skill_id, version, description,
                       shared, listed_by, installs, created_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                    """,
                    listing.id,
                    _scope(listing.tenant_id),
                    listing.skill_id,
                    listing.version,
                    listing.description,
                    listing.shared,
                    listing.listed_by,
                    listing.installs,
                    listing.created_at,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("marketplace: listing persist failed: %s", exc)
        raise MarketplaceError(f"上架失败：{exc}", status_code=500) from exc
    return listing


async def search_listings(
    tenant_id: str | None,
    *,
    q: str | None = None,
    domain: str | None = None,
    include_cross_tenant: bool = False,
    limit: int = 20,
) -> list[SkillListing]:
    """Search listings — own tenant always, other tenants only when ``shared``.

    ``include_cross_tenant`` is a request flag; a listing is still only
    returned when its own ``shared`` is TRUE (§7.3 default FALSE).
    """
    if not _is_postgres():
        rows: list[SkillListing] = []
        scopes = [_scope(tenant_id)]
        if include_cross_tenant:
            scopes = list(_LISTINGS.keys())
        for scope in scopes:
            for listing in _LISTINGS.get(scope, {}).values():
                if scope != _scope(tenant_id) and not listing.shared:
                    continue
                rows.append(listing)
    else:
        rows = await _pg_search_listings(
            tenant_id,
            q=q,
            domain=domain,
            include_cross_tenant=include_cross_tenant,
            limit=limit,
        )

    needle = (q or "").strip().lower()
    if needle:
        rows = [
            r
            for r in rows
            if needle in r.name.lower()
            or needle in r.description.lower()
            or needle in r.domain.lower()
        ]
    if domain:
        rows = [r for r in rows if r.domain == domain]
    rows.sort(key=lambda r: (r.rating, r.installs), reverse=True)
    return rows[:limit]


async def _pg_search_listings(
    tenant_id: str | None,
    *,
    q: str | None,
    domain: str | None,
    include_cross_tenant: bool,
    limit: int,
) -> list[SkillListing]:
    """PG search — joins ``skills`` for name/domain (listings carry neither)."""
    from forgeflow.database import get_pool

    where = "(l.tenant_id IS NOT DISTINCT FROM $1 OR (l.shared = TRUE AND $2))"
    args: list[Any] = [_scope(tenant_id), include_cross_tenant]
    if q:
        args.append(f"%{q.strip().lower()}%")
        where += f" AND (LOWER(COALESCE(s.name,'')) LIKE ${len(args)} OR LOWER(l.description) LIKE ${len(args)})"
    if domain:
        args.append(domain)
        where += f" AND COALESCE(s.domain,'') = ${len(args)}"

    sql = f"""
        SELECT l.id, l.tenant_id, l.skill_id, l.version, l.description,
               l.installs, l.created_at,
               COALESCE(l.shared, FALSE)   AS shared,
               l.listed_by,
               COALESCE(l.rating, 0)       AS rating,
               COALESCE(l.rating_count, 0) AS rating_count,
               COALESCE(s.name, '')        AS name,
               COALESCE(s.domain, '')      AS domain
        FROM skill_listings l
        LEFT JOIN skills s ON s.id = l.skill_id
        WHERE {where}
        ORDER BY COALESCE(l.rating, 0) DESC, l.installs DESC
        LIMIT {int(limit)}
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        records = await conn.fetch(sql, *args)
    return [
        SkillListing(
            id=str(r["id"]),
            tenant_id=str(r["tenant_id"]) if r["tenant_id"] else None,
            skill_id=str(r["skill_id"]),
            version=r["version"],
            name=r["name"],
            domain=r["domain"],
            description=r["description"] or "",
            shared=bool(r["shared"]),
            listed_by=r["listed_by"],
            rating=float(r["rating"] or 0.0),
            rating_count=int(r["rating_count"] or 0),
            installs=int(r["installs"] or 0),
            created_at=r["created_at"] or utcnow(),
        )
        for r in records
    ]


async def install_listing(
    tenant_id: str | None,
    listing_id: str,
    *,
    actor: str = "anonymous",
) -> dict[str, Any]:
    """Install a listing into the caller's tenant; bumps the install counter."""
    listing = await _find_listing(tenant_id, listing_id)
    if listing is None:
        raise MarketplaceError(f"listing '{listing_id}' not found", status_code=404)
    if listing.tenant_id != tenant_id and not listing.shared:
        raise MarketplaceError("该技能未共享，无法跨租户安装")

    listing.installs += 1
    await _bump_installs(listing)
    return {
        "installed": True,
        "listing": listing.to_dict(),
        "installed_by": actor,
    }


async def _find_listing(tenant_id: str | None, listing_id: str) -> SkillListing | None:
    if not _is_postgres():
        for scope, items in _LISTINGS.items():
            for listing in items.values():
                if listing.id == listing_id:
                    return listing
        return None

    from forgeflow.database import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT l.id, l.tenant_id, l.skill_id, l.version, l.description,
                   l.installs, l.created_at,
                   COALESCE(l.shared, FALSE)   AS shared,
                   l.listed_by,
                   COALESCE(l.rating, 0)       AS rating,
                   COALESCE(l.rating_count, 0) AS rating_count,
                   COALESCE(s.name, '')        AS name,
                   COALESCE(s.domain, '')      AS domain
            FROM skill_listings l
            LEFT JOIN skills s ON s.id = l.skill_id
            WHERE l.id = $1
            """,
            listing_id,
        )
    if row is None:
        return None
    return SkillListing(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]) if row["tenant_id"] else None,
        skill_id=str(row["skill_id"]),
        version=row["version"],
        name=row["name"],
        domain=row["domain"],
        description=row["description"] or "",
        shared=bool(row["shared"]),
        listed_by=row["listed_by"],
        rating=float(row["rating"] or 0.0),
        rating_count=int(row["rating_count"] or 0),
        installs=int(row["installs"] or 0),
        created_at=row["created_at"] or utcnow(),
    )


async def _bump_installs(listing: SkillListing) -> None:
    if not _is_postgres():
        return
    try:
        from forgeflow.database import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE skill_listings SET installs = installs + 1 WHERE id = $1",
                listing.id,
            )
    except Exception as exc:  # noqa: BLE001 — a counter must not fail an install
        logger.debug("marketplace: install counter bump skipped: %s", exc)


async def rate_listing(
    tenant_id: str | None,
    listing_id: str,
    score: int,
    *,
    user_id: str = "anonymous",
    comment: str = "",
) -> SkillListing:
    """Rate a listing (1–5) and recompute the running average."""
    if not 1 <= int(score) <= 5:
        raise MarketplaceError("评分必须为 1–5 的整数", status_code=422)

    listing = await _find_listing(tenant_id, listing_id)
    if listing is None:
        raise MarketplaceError(f"listing '{listing_id}' not found", status_code=404)

    total = listing.rating * listing.rating_count + int(score)
    listing.rating_count += 1
    listing.rating = total / listing.rating_count

    if not _is_postgres():
        _RATINGS.setdefault(listing_id, []).append(
            {"user_id": user_id, "score": int(score), "comment": comment}
        )
        return listing

    try:
        from forgeflow.database import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO skill_ratings (listing_id, tenant_id, user_id, score, comment)
                VALUES ($1,$2,$3,$4,$5)
                """,
                listing.id,
                _scope(tenant_id),
                user_id,
                int(score),
                comment,
            )
            await conn.execute(
                "UPDATE skill_listings SET rating=$2, rating_count=$3 WHERE id=$1",
                listing.id,
                listing.rating,
                listing.rating_count,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("marketplace: rating persist failed: %s", exc)
        raise MarketplaceError(f"评分失败：{exc}", status_code=500) from exc
    return listing
