"""Five-layer Memory scopes — partition, isolation, rules (docs §3.1 / P0-13)."""

from __future__ import annotations

import uuid

import pytest

from forgeflow.experience.memory_store import (
    clear_memory_entries,
    count_memories,
    list_memories,
    save_memory,
)
from forgeflow.experience.scopes import (
    MemoryScope,
    is_valid_scope,
    list_scopes,
    scope_namespace,
)

pytestmark = pytest.mark.asyncio


def _tenant() -> str:
    return f"t-mem-{uuid.uuid4().hex[:8]}"


def test_five_scopes_catalogue():
    scopes = list_scopes()
    assert {s["scope"] for s in scopes} == {"user", "team", "episodic", "semantic", "org"}
    assert all("writable_by" in s and "readable_by" in s for s in scopes)
    assert is_valid_scope("semantic") is True
    assert is_valid_scope("bogus") is False


def test_scope_namespace_is_tenant_prefixed():
    ns = scope_namespace(MemoryScope.USER, "acme", actor_id="u1")
    assert ns == "workspace/acme/user/u1"
    assert scope_namespace(MemoryScope.TEAM, "acme", team_id="t1") == "workspace/acme/team/t1"


async def test_scope_partitioned_write_and_read():
    clear_memory_entries()
    tenant = _tenant()

    await save_memory(tenant, "user", "私有偏好", actor_id="u1")
    await save_memory(tenant, "team", "团队共识", team_id="t1")
    await save_memory(tenant, "semantic", "稳定事实")

    assert await count_memories(tenant) == 3
    only_user = await list_memories(tenant, scope="user")
    assert [m.content for m in only_user] == ["私有偏好"]
    only_team = await list_memories(tenant, scope="team", team_id="t1")
    assert [m.content for m in only_team] == ["团队共识"]


async def test_scopes_are_isolated_per_tenant():
    clear_memory_entries()
    tenant_a = _tenant()
    tenant_b = _tenant()

    await save_memory(tenant_a, "org", "组织知识")
    assert await count_memories(tenant_a) == 1
    assert await count_memories(tenant_b) == 0


async def test_invalid_scope_falls_back_to_semantic():
    clear_memory_entries()
    tenant = _tenant()
    entry = await save_memory(tenant, "not-a-scope", "内容")
    assert entry.scope == "semantic"
