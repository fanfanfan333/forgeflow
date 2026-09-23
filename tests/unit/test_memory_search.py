"""INC2-14 — /memory/search merge (D1) + promotion endpoint (§2.17 / §2.6).

Asserts the memory backend serves ``/memory/search`` with the exact
``MemorySearchResult`` shape (no PostgreSQL pool / OpenAI needed) and that the
promotion router endpoint delegates to INC2-15 and returns the moved entry.
"""

from __future__ import annotations

import uuid

import pytest

from forgeflow.api.routers.memory import (
    MemoryPromoteRequest,
    promote_memory_entry,
    search_memory,
)
from forgeflow.config import get_settings
from forgeflow.experience.memory_store import clear_memory_entries, save_memory, search
from forgeflow.rbac.models import UserContext

pytestmark = pytest.mark.asyncio


def _tenant() -> str:
    return f"t-search-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _memory_backend(force_memory_backend):
    """The memory-search route is a *memory-backend* path (INC2 D1, §2.17).

    It serves results from the scope-partitioned in-process store and must be
    reachable with **no PostgreSQL pool**. Pin the backend so these two route
    cases exercise that path under any ``STORAGE_BACKEND`` — otherwise the
    postgres branch would demand a pool and 503.
    """
    return force_memory_backend



async def test_memory_store_search_returns_sorted_pairs():
    clear_memory_entries()
    tenant = _tenant()
    await save_memory(tenant, "semantic", "销售线索评分规则与权重")
    await save_memory(tenant, "semantic", "完全无关的另一段内容")

    pairs = await search(tenant, "销售线索评分", k=5)
    assert pairs
    similarities = [sim for _, sim in pairs]
    assert similarities == sorted(similarities, reverse=True)


async def test_memory_store_search_scope_filter():
    clear_memory_entries()
    tenant = _tenant()
    await save_memory(tenant, "user", "用户私有内容", actor_id="u1")
    await save_memory(tenant, "semantic", "语义内容")

    user_only = await search(tenant, "内容", k=5, scope="user")
    assert all(entry.scope == "user" for entry, _ in user_only)


async def test_search_route_memory_backend_without_pool():
    # Offline profile: the memory backend must not require a DB pool.
    assert get_settings().storage_backend.lower() != "postgres"

    clear_memory_entries()
    tenant = _tenant()
    await save_memory(tenant, "semantic", "hello world memory fact")

    results = await search_memory(
        q="hello world memory fact",
        k=5,
        namespace=None,
        scope=None,
        pool=None,
        workspace_id=None,
        tenant=tenant,
    )

    assert isinstance(results, list) and results
    top = results[0]
    # Field-for-field parity with the legacy MemorySearchResult.
    for field in ("id", "content", "similarity", "namespace", "metadata"):
        assert hasattr(top, field)
    assert top.content == "hello world memory fact"
    assert isinstance(top.similarity, float)
    assert top.namespace == f"workspace/{tenant}/semantic"


async def test_search_route_returns_empty_for_unknown_tenant():
    clear_memory_entries()
    results = await search_memory(
        q="nothing matches",
        k=5,
        namespace=None,
        scope=None,
        pool=None,
        workspace_id=None,
        tenant=_tenant(),
    )
    assert results == []


async def test_promote_endpoint_delegates_and_returns_entry():
    clear_memory_entries()
    from forgeflow.experience.promotion import clear_promotion_audit, list_promotion_audit

    clear_promotion_audit()
    tenant = _tenant()
    entry = await save_memory(tenant, "semantic", "可下沉的团队知识")

    response = await promote_memory_entry(
        entry.id,
        MemoryPromoteRequest(to_scope="team", reason="share with the team"),
        user=UserContext(user_id="u1", role="member"),
        tenant=tenant,
    )

    assert response.id == entry.id
    assert response.scope == "team"
    assert response.namespace == f"workspace/{tenant}/team/default"
    assert any(e.action == "memory.promote" for e in list_promotion_audit())


async def test_promote_endpoint_org_requires_admin():
    from fastapi import HTTPException

    clear_memory_entries()
    tenant = _tenant()
    entry = await save_memory(tenant, "semantic", "组织知识")

    with pytest.raises(HTTPException) as exc:
        await promote_memory_entry(
            entry.id,
            MemoryPromoteRequest(to_scope="org"),
            user=UserContext(user_id="u1", role="viewer"),
            tenant=tenant,
        )
    assert exc.value.status_code == 403
