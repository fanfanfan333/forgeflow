"""INC4 T6 — the ``/cost`` family must never 5xx in the offline (memory) profile.

Background (``目标.md`` §L6 U1 / ``docs/sop/08-FRAMEWORK-ALIGNMENT.md`` §1, §5)
------------------------------------------------------------------------------
``GET /cost/board`` and ``GET /cost/savings`` used to depend on
``api.dependencies.get_pool``, which raises ``503`` ("Database pool not
initialised") whenever no PostgreSQL pool exists. In the offline profile
(``STORAGE_BACKEND=memory``) that made the whole cost surface unusable, so the
SPA could not render KPI #3 「节省成本」 without erroring.

The routes now resolve their pool through ``cost.py::_optional_pool`` — the very
same "pool is optional offline" pattern already used by the ``/metrics`` family
(``metrics.py::_optional_pool``) — so a missing pool is a *normal* offline
condition, not a 503.

These tests pin that contract over the ASGI layer with **no** ``app.state.pool``.
They also assert the honesty rule: with no budgets and no spend the payload is
the truthful empty value (``has_data=false`` / ``amount=null``), never a
fabricated ``0`` or a stand-in figure.

They are intentionally *additive*: the frozen value/data-shape assertions live
in ``test_cost_api.py`` / ``test_cost_savings.py`` and are left untouched.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.routers import cost as cost_router
from forgeflow.cost.budget_service import BudgetService
from forgeflow.repositories.factory import reset_repositories
from forgeflow.repositories.memory.cost_repo import clear_cost_budget_store


@pytest.fixture(autouse=True)
def _clean_cost_state():
    """Start every case from an empty budget store + fresh repositories."""
    clear_cost_budget_store()
    reset_repositories()
    try:
        yield
    finally:
        clear_cost_budget_store()
        reset_repositories()


def _offline_client(workspace: str | None = None) -> TestClient:
    """A TestClient whose app has **no** ``state.pool`` — the offline profile.

    The real memory-profile server also runs without a pool, so this mirrors the
    production shape: ``_optional_pool`` must return ``None`` and the route must
    still answer ``200``. When ``workspace`` is given a middleware pins the
    request tenant to it, so the run/budget lookups are isolated and the
    empty-value assertions are deterministic regardless of test order.
    """
    app = FastAPI()

    if workspace is not None:

        @app.middleware("http")
        async def _pin_workspace(request, call_next):  # type: ignore[no-untyped-def]
            request.state.workspace_id = workspace
            return await call_next(request)

    app.include_router(cost_router.router, prefix="/cost")
    # The precondition under test: there is no pool for the route to depend on.
    assert getattr(app.state, "pool", None) is None
    return TestClient(app)


def _unique_tenant() -> str:
    return f"t-cost-mem-{uuid.uuid4().hex}"


def test_cost_board_memory_profile_is_200_not_5xx(force_memory_backend):
    """No pool + memory backend ⇒ 200, not the historical 503."""
    response = _offline_client(_unique_tenant()).get("/cost/board")

    assert response.status_code == 200
    body = response.json()
    # Honest empty value — no fabricated budget / spend.
    assert body["has_data"] is False
    assert body["budgets"] == []
    assert body["total_spent"] == 0.0
    assert body["total_limit"] is None
    assert body["total_pct"] is None
    assert body["currency"] == "CNY"


def test_cost_savings_memory_profile_is_200_not_5xx(force_memory_backend):
    """No pool + memory backend ⇒ 200 with a null amount (never 0)."""
    response = _offline_client(_unique_tenant()).get("/cost/savings")

    assert response.status_code == 200
    body = response.json()
    assert body["has_data"] is False
    assert body["amount"] is None
    assert body["baseline"] is None
    assert body["currency"] == "CNY"


def test_cost_family_never_5xx_without_a_pool(force_memory_backend):
    """Every route mounted on the cost router answers < 500 with no pool."""
    client = _offline_client(_unique_tenant())
    for path in ("/cost/board", "/cost/savings"):
        response = client.get(path)
        assert response.status_code == 200, f"{path} -> {response.status_code}"


def test_cost_board_memory_profile_still_reports_a_real_budget(force_memory_backend):
    """Positive control: the route actually computes — not a hard-wired empty.

    With a tenant budget configured for this workspace the board must flip to
    ``has_data=True`` and surface the limit, proving the offline path is a real
    read (memory store) rather than a blanket "return empty".
    """
    tenant = _unique_tenant()
    # BudgetService is async; the memory store is a plain in-process dict, so a
    # throwaway loop is enough to seed it before the (sync) TestClient reads it.
    import asyncio

    asyncio.run(BudgetService().set_budget(tenant, "tenant", 100.0))

    response = _offline_client(tenant).get("/cost/board")

    assert response.status_code == 200
    body = response.json()
    assert body["has_data"] is True
    assert body["total_limit"] == 100.0
    assert len(body["budgets"]) == 1
    assert body["budgets"][0]["scope"] == "tenant"
