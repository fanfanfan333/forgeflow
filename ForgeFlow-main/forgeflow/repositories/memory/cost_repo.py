"""In-memory CostBudget repository (INC2-03).

Keyed ``tenant -> (scope, scope_id) -> row``, mirroring the PostgreSQL
``UNIQUE (tenant_id, scope, scope_id)`` constraint so an upsert behaves the
same on both backends.
"""

from __future__ import annotations

import asyncio

from forgeflow.cost.models import CostBudgetRecord
from forgeflow.repositories.base import TenantScopedRepository

_BUDGETS: dict[str, dict[tuple[str, str | None], CostBudgetRecord]] = {}
_LOCK = asyncio.Lock()


def clear_cost_budget_store() -> None:
    """Reset all in-memory budget state. Test helper only."""
    _BUDGETS.clear()


class MemoryCostBudgetRepository(TenantScopedRepository):
    """Dict-backed ``CostBudgetRepository`` (offline profile)."""

    async def get(
        self,
        tenant_id: str | None,
        scope: str,
        scope_id: str | None = None,
    ) -> CostBudgetRecord | None:
        return _BUDGETS.get(self.scope_key(tenant_id), {}).get((scope, scope_id))

    async def upsert(self, budget: CostBudgetRecord) -> CostBudgetRecord:
        key = self.scope_key(budget.tenant_id)
        async with _LOCK:
            _BUDGETS.setdefault(key, {})[budget.key] = budget
        return budget

    async def list_by_scope(
        self,
        tenant_id: str | None,
        scope: str | None = None,
    ) -> list[CostBudgetRecord]:
        rows = list(_BUDGETS.get(self.scope_key(tenant_id), {}).values())
        if scope:
            rows = [r for r in rows if r.scope == scope]
        rows.sort(key=lambda r: (r.scope, r.scope_id or "", r.created_at), reverse=True)
        return rows

    async def delete(
        self,
        tenant_id: str | None,
        scope: str,
        scope_id: str | None = None,
    ) -> bool:
        bucket = _BUDGETS.get(self.scope_key(tenant_id))
        if not bucket:
            return False
        async with _LOCK:
            return bucket.pop((scope, scope_id), None) is not None
