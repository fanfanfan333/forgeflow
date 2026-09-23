"""INC2 A1 — cost board + savings API (frozen contract, honesty rule).

The SPA is built against these exact field names, so the tests pin the shape as
well as the values. The honesty rule (§7.1): no data ⇒ ``has_data=False`` and
``amount=null`` — never a fabricated ``0``.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.routers import cost as cost_router
from forgeflow.api.routers.cost import build_board_payload, build_savings_payload
from forgeflow.config import get_settings
from forgeflow.cost.budget_service import BudgetService
from forgeflow.repositories.memory.cost_repo import clear_cost_budget_store


@pytest.fixture(autouse=True)
def _isolate_cost_budgets(pg_purge):
    """Clear the shared ``cost_budgets`` table around each case (PG profile).

    The board/savings payloads count budget rows *exactly*, so a shared dev table
    carrying rows left by an earlier test or run makes the "empty ⇒ honest
    no-data" contract fail (the real defect behind the ``5 == 1`` / ``True is
    False`` failures). Under the memory profile ``pg_purge`` is a no-op and the
    in-process store is cleared by ``clear_cost_budget_store`` inside each test.
    """
    pg_purge("cost_budgets")
    yield
    pg_purge("cost_budgets")



# --------------------------------------------------------------------------- #
# GET /cost/board                                                              #
# --------------------------------------------------------------------------- #

async def test_board_without_budgets_is_empty_and_honest():
    clear_cost_budget_store()
    payload = await build_board_payload("t-cost-empty")

    assert payload["has_data"] is False
    assert payload["budgets"] == []
    assert payload["currency"] == "CNY"
    assert payload["tenant_id"] == "t-cost-empty"
    assert payload["total_limit"] is None
    assert payload["total_spent"] == 0.0
    assert payload["total_pct"] is None
    assert payload["level"] == "ok"


async def test_board_with_tenant_budget_reports_limit():
    clear_cost_budget_store()
    tenant = "t-cost-tenant"
    await BudgetService().set_budget(tenant, "tenant", 100.0)

    payload = await build_board_payload(tenant)

    assert payload["has_data"] is True
    assert payload["total_limit"] == 100.0
    assert payload["total_spent"] == 0.0
    assert payload["total_pct"] == 0.0
    assert payload["level"] == "ok"
    assert len(payload["budgets"]) == 1
    row = payload["budgets"][0]
    assert row == {
        "scope": "tenant",
        "scope_id": None,
        "limit": 100.0,
        "spent": 0.0,
        "pct": 0.0,
        "level": "ok",
    }


async def test_board_orders_the_three_tiers():
    clear_cost_budget_store()
    tenant = "t-cost-three"
    service = BudgetService()
    await service.set_budget(tenant, "task", 100.0, scope_id="task-x")
    await service.set_budget(tenant, "team", 300.0, scope_id="team-a")
    await service.set_budget(tenant, "tenant", 500.0)

    payload = await build_board_payload(tenant)

    assert [row["scope"] for row in payload["budgets"]] == ["tenant", "team", "task"]
    # The overall cap comes from the tenant-level budget.
    assert payload["total_limit"] == 500.0


# --------------------------------------------------------------------------- #
# GET /cost/savings                                                            #
# --------------------------------------------------------------------------- #

async def test_savings_without_baseline_is_null_not_zero():
    clear_cost_budget_store()
    payload = await build_savings_payload("t-cost-empty")

    assert payload["has_data"] is False
    assert payload["amount"] is None
    assert payload["baseline"] is None
    assert payload["actual"] == 0.0
    assert payload["currency"] == "CNY"
    assert payload["period"] == "current"
    assert payload["multiplier"] == float(get_settings().cost_savings_baseline_multiplier)


# --------------------------------------------------------------------------- #
# HTTP layer — the frozen contract over the wire                               #
# --------------------------------------------------------------------------- #

def _client() -> TestClient:
    app = FastAPI()
    app.include_router(cost_router.router, prefix="/cost")
    return TestClient(app)


def test_board_route_returns_frozen_contract():
    clear_cost_budget_store()
    response = _client().get("/cost/board")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "has_data",
        "currency",
        "tenant_id",
        "total_limit",
        "total_spent",
        "total_pct",
        "level",
        "budgets",
    }
    assert body["has_data"] is False


def test_savings_route_returns_frozen_contract():
    clear_cost_budget_store()
    response = _client().get("/cost/savings")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "has_data",
        "currency",
        "amount",
        "baseline",
        "actual",
        "multiplier",
        "period",
    }
    assert body["has_data"] is False
    assert body["amount"] is None
