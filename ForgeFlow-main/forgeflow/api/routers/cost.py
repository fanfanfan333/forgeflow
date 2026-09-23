"""Cost optimization routes — three-tier budget board + savings (INC2 A1).

User-visible surface for the "企业省钱" capability (user ask #11). The router
holds no aggregation logic of its own: it reads the CostBudget repository
through :class:`~forgeflow.cost.budget_service.BudgetService` and grades each
scope with the same ratio thresholds the service uses, and it computes savings
via :func:`forgeflow.cost.savings.savings_from_totals`.

Honesty rule (architecture §7.1, hard requirement)
--------------------------------------------------
When there is no data we report the truth, never a fabricated number:

  * ``GET /cost/board``   — ``has_data=False`` and an empty ``budgets`` list when
    no budget is configured and no spend is recorded.
  * ``GET /cost/savings`` — ``has_data=False`` and ``amount=null`` whenever there
    is no previous-period baseline. Never ``0`` and never the current cost.

Frozen response contract (the SPA is built against these exact field names)::

    GET /cost/board   → {has_data, currency, tenant_id, total_limit, total_spent,
                         total_pct, level, budgets:[{scope, scope_id, limit,
                         spent, pct, level}]}
    GET /cost/savings → {has_data, currency, amount, baseline, actual,
                         multiplier, period}

Importing this module never opens a connection and never imports asyncpg.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request

from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.config import get_settings
from forgeflow.cost.budget_service import BudgetService
from forgeflow.cost.savings import SavingsResult, savings_from_totals

logger = logging.getLogger(__name__)
router = APIRouter()

__all__ = ["router", "build_board_payload", "build_savings_payload", "SAVINGS_WINDOW_DAYS"]

#: Length of the current comparison window. The previous window is the same
#: length, immediately before it (savings baseline, §7.1).
SAVINGS_WINDOW_DAYS = 30

#: Stable display order for the three budget tiers.
_SCOPE_ORDER: dict[str, int] = {"tenant": 0, "team": 1, "task": 2}


def _optional_pool(request: Request) -> Any | None:
    """Return the asyncpg pool when one was created, else ``None``.

    Unlike ``api.dependencies.get_pool`` this must **not** raise in the offline
    (memory) profile — the cost board and savings are meaningful without
    PostgreSQL, so a missing pool is a normal condition, not a 503.
    """
    return getattr(request.app.state, "pool", None)


async def _period_totals(
    tenant_id: str, *, window_days: int, pool: Any | None
) -> tuple[float | None, float]:
    """Return ``(previous_period_cost, current_period_cost)`` for a tenant.

    Delegates to :mod:`forgeflow.cost.ledger`, which reads the recorded run
    cost from the active backend:

      * **memory** — the tenant-scoped hub run store (``run_task`` records each
        run's ``total_cost_usd``; INC2 A1 producer).
      * **postgres** — ``workflow_runs.total_cost_usd`` over the same windows.

    ``previous_period_cost`` is ``None`` when the previous window recorded no
    *billable* cost — the savings maths then renders 「—」 (never a fabricated
    ``0``). A ledger error degrades to "no data" so the route never 500s.
    """
    try:
        from forgeflow.cost.ledger import period_totals

        return await period_totals(tenant_id, window_days=window_days, pool=pool)
    except Exception as exc:  # noqa: BLE001 — the board must degrade, never 500
        logger.warning("cost ledger unavailable, reporting no data: %s", exc)
        return None, 0.0


def _pct(spent: float, limit: float | None) -> float | None:
    """``spent / limit`` as a fraction, or ``None`` when there is no usable limit."""
    if limit is None or limit <= 0:
        return None
    return spent / limit


def _grade_level(spent: float, limit: float | None, warn_ratio: float | None) -> str:
    """Grade one scope, mirroring ``BudgetService`` ratio thresholds (§2.1).

    ``ratio >= 1.0`` → ``exceeded``; ``warn_ratio <= ratio < 1.0`` → ``warn``;
    otherwise ``ok``. A zero/absent limit is treated as "no budget to breach".
    """
    if limit is None:
        return "ok"
    if limit > 0:
        ratio = spent / limit
    else:
        ratio = 0.0 if spent <= 0 else 1.0
    if ratio >= 1.0:
        return "exceeded"
    effective_warn = (
        float(warn_ratio) if warn_ratio is not None else float(get_settings().cost_warn_ratio)
    )
    return "warn" if ratio >= effective_warn else "ok"


async def build_board_payload(
    tenant_id: str,
    *,
    pool: Any | None = None,
    service: BudgetService | None = None,
) -> dict[str, Any]:
    """Assemble the three-tier budget board payload for one tenant."""
    service = service or BudgetService()
    settings = get_settings()
    currency = getattr(settings, "cost_currency", "CNY")

    budgets = await service.list_budgets(tenant_id)
    _previous, total_spent = await _period_totals(
        tenant_id, window_days=SAVINGS_WINDOW_DAYS, pool=pool
    )
    total_spent = float(total_spent or 0.0)

    tenant_budget = next(
        (b for b in budgets if b.scope == "tenant" and b.scope_id is None), None
    )
    total_limit = float(tenant_budget.limit_amount) if tenant_budget is not None else None

    rows: list[dict[str, Any]] = []
    for budget in budgets:
        limit = float(budget.limit_amount) if budget.limit_amount is not None else None
        # Only the tenant-level ledger is attributable today; team/task spend
        # has no per-scope source, so it reports 0.0 (an empty ledger) rather
        # than a guess attributed to the wrong scope.
        spent = total_spent if budget.scope == "tenant" else 0.0
        rows.append(
            {
                "scope": budget.scope,
                "scope_id": budget.scope_id,
                "limit": limit,
                "spent": spent,
                "pct": _pct(spent, limit),
                "level": _grade_level(spent, limit, budget.warn_ratio),
            }
        )
    rows.sort(key=lambda row: (_SCOPE_ORDER.get(row["scope"], 9), row["scope_id"] or ""))

    # Reuse the service for the top-level verdict — it binds the strictest
    # applicable budget (task > team > tenant, §2.1).
    decision = await service.evaluate_budget(tenant_id, total_spent)

    return {
        "has_data": bool(budgets) or total_spent > 0,
        "currency": currency,
        "tenant_id": tenant_id,
        "total_limit": total_limit,
        "total_spent": total_spent,
        "total_pct": _pct(total_spent, total_limit),
        "level": decision.level,
        "budgets": rows,
    }


async def build_savings_payload(tenant_id: str, *, pool: Any | None = None) -> dict[str, Any]:
    """Assemble the savings-vs-previous-period payload for one tenant (§7.1)."""
    settings = get_settings()
    multiplier = float(settings.cost_savings_baseline_multiplier)
    previous, current = await _period_totals(
        tenant_id, window_days=SAVINGS_WINDOW_DAYS, pool=pool
    )
    result: SavingsResult = savings_from_totals(previous, current, multiplier=multiplier)
    return {
        "has_data": result.has_baseline,
        "currency": getattr(settings, "cost_currency", "CNY"),
        "amount": result.amount,
        "baseline": result.baseline,
        "actual": result.actual,
        "multiplier": multiplier,
        "period": "current",
    }


@router.get("/board")
async def cost_board(
    request: Request,
    tenant: str = Depends(resolve_tenant),
) -> dict[str, Any]:
    """Three-tier budget board (tenant / team / task)."""
    return await build_board_payload(tenant, pool=_optional_pool(request))


@router.get("/savings")
async def cost_savings(
    request: Request,
    tenant: str = Depends(resolve_tenant),
) -> dict[str, Any]:
    """Saved amount vs. the previous period's like-for-like cost."""
    return await build_savings_payload(tenant, pool=_optional_pool(request))
