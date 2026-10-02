"""INC40 — 记忆「归档」接线钉子（A 档 memory）。

`KnowledgeView` 的记忆列表来自进程内 hub store（`experience.memory_store`），而
`DELETE /memory/{id}` 打的是 pgvector `memory_vectors` —— 两套 id 空间不相交，无法
直接把 DELETE 挂到列表（每行都会 404，见设计 §0.2）。故 INC40 接线已存在的 hub
**归档**能力为 `POST /memory/{memory_id}/archive`：

  * 归档 → 200 + 真实 JSON 体（**非 204**）；
  * 随后 `GET /memory` 默认列表**不含**该条；
  * 但条目仍在（`include_archived=True` 可见）⇒ 证明是**标记式归档**、绝非物理删除；
  * 跨租户 / 不存在 → **404**（不泄露存在性）。

档位声明：断言内存档行为（进程内 hub store），按纪律显式声明 `force_memory_backend`。

引文纪律：一律 `file.py::symbol`，不用行号。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from forgeflow.api.routers.memory import (
    archive_memory_entry,
    list_memory_entries,
)
from forgeflow.experience.memory_store import (
    clear_memory_entries,
    list_memories,
    save_memory,
)

pytestmark = pytest.mark.asyncio


def _t() -> str:
    return f"t-inc40-mem-{uuid.uuid4().hex[:10]}"


@pytest.fixture(autouse=True)
def _clean_memory(force_memory_backend):
    clear_memory_entries()
    yield
    clear_memory_entries()


async def test_archive_hides_entry_from_default_list_but_does_not_delete_it():
    tenant = _t()
    entry = await save_memory(tenant, "semantic", "归档前可见的记忆")

    # 归档前：默认列表含该条。
    before = await list_memory_entries(
        scope=None, team_id=None, limit=50, offset=0, tenant=tenant
    )
    assert entry.id in {item.id for item in before.items}

    # 归档：真实路由，返回 JSON 体（非 204）。
    result = await archive_memory_entry(entry.id, tenant=tenant)
    assert result == {"archived": True, "memory_id": entry.id}

    # 归档后：默认列表不再包含该条。
    after = await list_memory_entries(
        scope=None, team_id=None, limit=50, offset=0, tenant=tenant
    )
    assert entry.id not in {item.id for item in after.items}

    # 但它仍存在（标记式归档，绝不物理删除 —— INC9 §2.2.6）。
    everything = await list_memories(tenant, include_archived=True)
    assert entry.id in {e.id for e in everything}


async def test_archive_cross_tenant_is_404():
    owner = _t()
    other = _t()
    entry = await save_memory(owner, "semantic", "别的租户看不到的记忆")

    with pytest.raises(HTTPException) as ei:
        await archive_memory_entry(entry.id, tenant=other)
    assert ei.value.status_code == 404

    # 原租户的那条仍在默认列表里（跨租户归档是 no-op，不是「成功」）。
    listing = await list_memory_entries(
        scope=None, team_id=None, limit=50, offset=0, tenant=owner
    )
    assert entry.id in {item.id for item in listing.items}


async def test_archive_unknown_id_is_404():
    tenant = _t()
    with pytest.raises(HTTPException) as ei:
        await archive_memory_entry("does-not-exist", tenant=tenant)
    assert ei.value.status_code == 404
