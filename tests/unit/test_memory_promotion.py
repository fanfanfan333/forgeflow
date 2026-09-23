"""INC2-15 — Org Memory promotion (docs/sop/05-ARCHITECTURE-INC2.md §2.6).

Covers the promotion rules (promotable source, target ∈ {team, org}, admin for
org), namespace recomputation, and the mandatory audit event.
"""

from __future__ import annotations

import uuid

import pytest

from forgeflow.experience.memory_store import clear_memory_entries, get_memory, save_memory
from forgeflow.experience.promotion import (
    PROMOTABLE_TARGETS,
    PromotionError,
    clear_promotion_audit,
    list_promotion_audit,
    promote_memory,
)

pytestmark = pytest.mark.asyncio


def _tenant() -> str:
    return f"t-promo-{uuid.uuid4().hex[:8]}"


async def test_promotable_targets_are_team_and_org():
    assert PROMOTABLE_TARGETS == {"team", "org"}


async def test_promote_semantic_to_team_recomputes_namespace():
    clear_memory_entries()
    tenant = _tenant()
    entry = await save_memory(tenant, "semantic", "可复用的团队知识")
    assert entry.namespace == f"workspace/{tenant}/semantic"

    moved = await promote_memory(
        tenant, entry.id, "semantic", "team", actor_id="u1", role="member", reason="share"
    )
    assert moved.scope == "team"
    assert moved.namespace == f"workspace/{tenant}/team/default"
    # The store reflects the move (same object).
    stored = await get_memory(tenant, entry.id)
    assert stored is not None and stored.scope == "team"


async def test_promote_to_team_with_team_id():
    clear_memory_entries()
    tenant = _tenant()
    entry = await save_memory(tenant, "user", "个人知识", actor_id="u1")
    moved = await promote_memory(
        tenant, entry.id, "user", "team", role="member", team_id="growth"
    )
    assert moved.namespace == f"workspace/{tenant}/team/growth"
    assert moved.team_id == "growth"


async def test_episodic_and_org_are_not_promotable():
    clear_memory_entries()
    tenant = _tenant()
    episodic = await save_memory(tenant, "episodic", "运行轨迹")
    with pytest.raises(PromotionError) as exc:
        await promote_memory(tenant, episodic.id, "episodic", "team")
    assert exc.value.status_code == 400

    org = await save_memory(tenant, "org", "已有组织知识")
    with pytest.raises(PromotionError) as exc2:
        await promote_memory(tenant, org.id, "org", "team")
    assert exc2.value.status_code == 400


async def test_promote_to_org_requires_admin():
    clear_memory_entries()
    tenant = _tenant()
    entry = await save_memory(tenant, "semantic", "组织级知识")

    with pytest.raises(PromotionError) as exc:
        await promote_memory(tenant, entry.id, "semantic", "org", role="member")
    assert exc.value.status_code == 403

    moved = await promote_memory(tenant, entry.id, "semantic", "org", role="admin")
    assert moved.scope == "org"
    assert moved.namespace == f"workspace/{tenant}/org"


async def test_invalid_target_scope_rejected():
    clear_memory_entries()
    tenant = _tenant()
    entry = await save_memory(tenant, "semantic", "x")
    with pytest.raises(PromotionError) as exc:
        await promote_memory(tenant, entry.id, "semantic", "user")
    assert exc.value.status_code == 400


async def test_from_scope_mismatch_rejected():
    clear_memory_entries()
    tenant = _tenant()
    entry = await save_memory(tenant, "semantic", "y")
    with pytest.raises(PromotionError) as exc:
        await promote_memory(tenant, entry.id, "user", "team")
    assert exc.value.status_code == 400


async def test_missing_memory_is_404():
    clear_memory_entries()
    with pytest.raises(PromotionError) as exc:
        await promote_memory(_tenant(), "does-not-exist", "semantic", "team")
    assert exc.value.status_code == 404


async def test_audit_event_written_on_promotion():
    clear_memory_entries()
    clear_promotion_audit()
    tenant = _tenant()
    entry = await save_memory(tenant, "semantic", "audit me")

    await promote_memory(
        tenant, entry.id, "semantic", "team", actor_id="u9", reason="cross-team reuse"
    )

    events = list_promotion_audit()
    assert events
    event = events[-1]
    assert event.action == "memory.promote"
    assert event.actor == "u9"
    assert event.resource == entry.id
    assert event.metadata["from"] == "semantic"
    assert event.metadata["to"] == "team"
    assert event.metadata["reason"] == "cross-team reuse"
