"""INC2-12 — skill-level marketplace: publish (dual pre-check), search, install, rate.

§7.3: ``shared`` defaults to FALSE, and flipping it cross-tenant requires the
explicit ``approve:skills`` grant. Publishing runs DLP + the trust baseline first.
"""

from __future__ import annotations

import uuid

import pytest

from forgeflow.skills.marketplace_bridge import (
    MarketplaceError,
    install_listing,
    publish_listing,
    rate_listing,
    reset_marketplace,
    search_listings,
)
from forgeflow.skills.models import SkillRecord

pytestmark = pytest.mark.asyncio

ADMIN = ["*:*"]
MANAGER = ["read:skills", "write:skills", "approve:skills"]
VIEWER = ["read:skills"]


def _tenant() -> str:
    """A fresh, *non-UUID* tenant slug per call.

    Tenant ids are opaque strings (``RequestContext.tenant_id: str``); since
    migration 013 the PostgreSQL ``tenant_id`` columns are ``TEXT``, so a slug
    like ``"t-mkt-<hex>"`` is stored verbatim on every backend. Using a
    non-UUID slug is deliberate — it exercises the exact case that used to leak:
    the retired UUID coercion collapsed every slug tenant onto one shared
    ``NULL`` bucket, so ``"t-mkt-a"`` and ``"t-mkt-b"`` were indistinguishable
    on PostgreSQL. A random suffix keeps two tenants distinct within one test.
    """
    return f"t-mkt-{uuid.uuid4().hex[:12]}"


@pytest.fixture(autouse=True)
def _isolate_marketplace(pg_purge):
    """Clear the shared marketplace tables around each case in the PG profile.

    ``skill_listings`` is a process-shared table; without this an earlier test
    (or a previous run) leaks rows into this tenant's searches — the real defect
    behind the "Left contains N more items" failures. ``skill_ratings`` is
    purged first so the (optional) FK never blocks the listing delete. Under the
    memory profile ``pg_purge`` is a no-op and ``reset_marketplace`` handles it.
    """
    pg_purge("skill_ratings", "skill_listings")
    yield
    pg_purge("skill_ratings", "skill_listings")



async def _seed(tenant: str, name: str = "销售分析技能", domain: str = "sales") -> str:
    from forgeflow.repositories import get_skill_repository

    skill = SkillRecord(
        tenant_id=tenant,
        name=name,
        domain=domain,
        description=f"{name} 描述",
        status="published",
        current_version="1.0.0",
    )
    await get_skill_repository().create_skill(skill)
    return skill.id


async def test_publish_succeeds_and_defaults_to_private():
    reset_marketplace()
    tenant = _tenant()
    skill_id = await _seed(tenant)

    listing = await publish_listing(
        tenant, skill_id, actor="u-1", actor_permissions=MANAGER
    )

    assert listing.shared is False, "shared must default to FALSE (§7.3)"
    assert listing.listed_by == "u-1"
    assert listing.name == "销售分析技能"

    found = await search_listings(tenant)
    assert [l.skill_id for l in found] == [skill_id]


async def test_publish_rejected_by_dlp():
    """A spec carrying PII never reaches the listing table."""
    reset_marketplace()
    tenant = _tenant()
    skill_id = await _seed(tenant, name="含敏感信息技能")

    from forgeflow.repositories import get_skill_repository

    skill = await get_skill_repository().get_skill(tenant, skill_id)
    skill.description = "联系 张三 邮箱 zhangsan@example.com"

    with pytest.raises(MarketplaceError) as excinfo:
        await publish_listing(
            tenant,
            skill_id,
            actor="u-1",
            actor_permissions=ADMIN,
            spec={"prompt": "邮箱 zhangsan@example.com，身份证 11010119900307123X"},
        )

    assert excinfo.value.status_code == 403
    assert "敏感信息" in excinfo.value.detail
    assert await search_listings(tenant) == []


async def test_publish_rejected_by_trust_baseline():
    """Off-catalogue tools / privilege escalation → 403."""
    reset_marketplace()
    tenant = _tenant()
    skill_id = await _seed(tenant, name="越权技能")

    with pytest.raises(MarketplaceError) as excinfo:
        await publish_listing(
            tenant,
            skill_id,
            actor="u-1",
            actor_permissions=VIEWER,
            spec={"tools": ["evil.exec"], "required_permissions": ["write:policies"]},
        )

    assert excinfo.value.status_code == 403
    assert "evil.exec" in excinfo.value.detail
    assert await search_listings(tenant) == []


async def test_shared_requires_the_explicit_grant():
    reset_marketplace()
    tenant = _tenant()
    skill_id = await _seed(tenant, name="待共享技能")

    # A viewer may not publish cross-tenant.
    with pytest.raises(MarketplaceError) as excinfo:
        await publish_listing(
            tenant, skill_id, actor="u-v", actor_permissions=VIEWER, shared=True
        )
    assert "approve:skills" in excinfo.value.detail

    # With the grant it works.
    listing = await publish_listing(
        tenant, skill_id, actor="u-m", actor_permissions=MANAGER, shared=True
    )
    assert listing.shared is True


async def test_cross_tenant_search_only_returns_shared():
    reset_marketplace()
    owner = _tenant()
    other = _tenant()
    shared_id = await _seed(owner, name="共享技能")
    private_id = await _seed(owner, name="私有技能")

    await publish_listing(owner, shared_id, actor="u-m", actor_permissions=MANAGER, shared=True)
    await publish_listing(owner, private_id, actor="u-m", actor_permissions=MANAGER)

    # Another tenant sees only the shared one.
    visible = await search_listings(other, include_cross_tenant=True)
    assert [l.skill_id for l in visible] == [shared_id]

    # ...and sees nothing when not asking for cross-tenant.
    assert await search_listings(other) == []


async def test_install_and_rate():
    reset_marketplace()
    tenant = _tenant()
    skill_id = await _seed(tenant, name="可安装技能")
    listing = await publish_listing(
        tenant, skill_id, actor="u-m", actor_permissions=MANAGER, shared=True
    )

    result = await install_listing(tenant, listing.id, actor="u-2")
    assert result["installed"] is True
    assert result["listing"]["installs"] == 1

    rated = await rate_listing(tenant, listing.id, 5, user_id="u-2")
    assert rated.rating_count == 1
    assert rated.rating == pytest.approx(5.0)

    rated = await rate_listing(tenant, listing.id, 3, user_id="u-3")
    assert rated.rating_count == 2
    assert rated.rating == pytest.approx(4.0)

    with pytest.raises(MarketplaceError) as excinfo:
        await rate_listing(tenant, listing.id, 9, user_id="u-4")
    assert excinfo.value.status_code == 422


async def test_install_of_a_private_listing_from_another_tenant_is_refused():
    reset_marketplace()
    owner = _tenant()
    other = _tenant()
    skill_id = await _seed(owner, name="私有技能")
    listing = await publish_listing(owner, skill_id, actor="u-m", actor_permissions=MANAGER)

    with pytest.raises(MarketplaceError) as excinfo:
        await install_listing(other, listing.id, actor="u-x")
    assert excinfo.value.status_code == 403
    assert "未共享" in excinfo.value.detail


async def test_publish_unknown_skill_is_404():
    reset_marketplace()
    with pytest.raises(MarketplaceError) as excinfo:
        await publish_listing(_tenant(), "does-not-exist", actor_permissions=ADMIN)
    assert excinfo.value.status_code == 404
