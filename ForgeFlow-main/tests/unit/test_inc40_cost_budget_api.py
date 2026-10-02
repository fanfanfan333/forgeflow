"""INC40 — 成本预算「建 → 读 → 删」端到端闭环钉子（A 档 memory）。

背景：`BudgetService.set_budget` 属「有实现、生产零接线」，三级预算此前**只能读、
无法经任何 API 创建**。INC40 把 `POST /cost/budgets`（upsert）+ `DELETE
/cost/budgets/{scope}` 接成真实路由（属**接线**，非造能力）。本用例经**真实路由函数**
钉住闭环：

  * 建（POST）后，`GET /cost/board` 含该行且 `has_data=True`；
  * 删（DELETE）→ 200 JSON 体；再删 → **404**（诚实：不存在不假装成功）；
  * 跨租户删 → **404**（不泄露存在性）；
  * 非法 scope / team 缺 scope_id → **422**。

档位声明：断言内存档行为（进程内预算表），按纪律显式声明 `force_memory_backend`。

引文纪律：一律 `file.py::symbol`，不用行号。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from forgeflow.api.routers.cost import (
    BudgetUpsertRequest,
    build_board_payload,
    delete_budget as delete_budget_route,
    upsert_budget as upsert_budget_route,
)
from forgeflow.repositories.memory.cost_repo import clear_cost_budget_store

pytestmark = pytest.mark.asyncio


def _t() -> str:
    return f"t-inc40-cost-{uuid.uuid4().hex[:10]}"


@pytest.fixture(autouse=True)
def _clean_budgets(force_memory_backend):
    # 进程内预算表跨用例共享 ⇒ 每例前后清空，保证「空即诚实无数据」的断言立足。
    clear_cost_budget_store()
    yield
    clear_cost_budget_store()


async def test_create_read_delete_closure():
    tenant = _t()

    # 建：POST /cost/budgets（tenant 级，scope_id 省略 ⇒ None）。
    created = await upsert_budget_route(
        BudgetUpsertRequest(scope="tenant", limit_amount=100.0), tenant=tenant
    )
    assert created["scope"] == "tenant"
    assert created["scope_id"] is None
    assert created["limit_amount"] == 100.0

    # 读：GET /cost/board 含该行且 has_data=True（不再是有实现、零接线的空壳）。
    board = await build_board_payload(tenant)
    assert board["has_data"] is True
    assert board["total_limit"] == 100.0
    assert [row["scope"] for row in board["budgets"]] == ["tenant"]

    # 删：DELETE /cost/budgets/tenant → 真实 JSON 体（非 204）。
    removed = await delete_budget_route("tenant", scope_id=None, tenant=tenant)
    assert removed == {"deleted": True, "scope": "tenant", "scope_id": None}

    # 删后看板回到诚实空态。
    after = await build_board_payload(tenant)
    assert after["has_data"] is False
    assert after["budgets"] == []


async def test_delete_missing_budget_is_404():
    tenant = _t()
    with pytest.raises(HTTPException) as ei:
        await delete_budget_route("tenant", scope_id=None, tenant=tenant)
    assert ei.value.status_code == 404


async def test_delete_cross_tenant_is_404_and_leaves_owner_intact():
    owner = _t()
    other = _t()
    await upsert_budget_route(
        BudgetUpsertRequest(scope="team", scope_id="team-a", limit_amount=50.0),
        tenant=owner,
    )

    # 别的租户删同一自然键 → 404（不泄露存在性）。
    with pytest.raises(HTTPException) as ei:
        await delete_budget_route("team", scope_id="team-a", tenant=other)
    assert ei.value.status_code == 404

    # 原租户的预算仍在。
    board = await build_board_payload(owner)
    assert [(r["scope"], r["scope_id"]) for r in board["budgets"]] == [("team", "team-a")]


async def test_invalid_scope_and_missing_scope_id_are_422():
    tenant = _t()
    with pytest.raises(HTTPException) as bad_scope:
        await upsert_budget_route(
            BudgetUpsertRequest(scope="galaxy", limit_amount=10.0), tenant=tenant
        )
    assert bad_scope.value.status_code == 422

    with pytest.raises(HTTPException) as missing_id:
        await upsert_budget_route(
            BudgetUpsertRequest(scope="team", limit_amount=10.0), tenant=tenant
        )
    assert missing_id.value.status_code == 422
