"""PostgreSQL CostBudget repository (INC2-03) — asyncpg, lazy pool.

Talks to the ``cost_budgets`` table created by migration ``011``. The
``tenant_id`` column is an opaque ``TEXT`` (since migration ``013``): the tenant
id is stored exactly as the application hands it, normalised only through
``scope_key`` so a missing tenant lands in the well-defined default bucket
rather than a shared ``NULL`` one.
"""

from __future__ import annotations

import json
from typing import Any

from forgeflow.cost.models import DEFAULT_EXCEED_ACTIONS, CostBudgetRecord
from forgeflow.repositories.base import TenantScopedRepository, utcnow


def _affected_rows(status: Any) -> int:
    """Row count from an asyncpg command tag (``"UPDATE 1"`` → ``1``)."""
    if not status:
        return 0
    parts = str(status).split()
    if len(parts) < 2 or not parts[0].isalpha():
        return 0
    try:
        return int(parts[-1])
    except ValueError:
        return 0


class PgCostBudgetRepository(TenantScopedRepository):
    """asyncpg-backed ``CostBudgetRepository``."""

    def __init__(self, default_tenant: str = "default", pool: Any | None = None) -> None:
        super().__init__(default_tenant)
        self._pool = pool

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        from forgeflow.database import get_pool

        return await get_pool()

    @staticmethod
    def _to_record(row: Any) -> CostBudgetRecord:
        d = dict(row)
        on_exceed = d.get("on_exceed")
        if isinstance(on_exceed, str):
            try:
                parsed = json.loads(on_exceed)
            except (TypeError, ValueError):
                parsed = list(DEFAULT_EXCEED_ACTIONS)
            on_exceed = [str(a) for a in parsed] if isinstance(parsed, list) else list(
                DEFAULT_EXCEED_ACTIONS
            )
        return CostBudgetRecord(
            id=str(d["id"]),
            tenant_id=str(d["tenant_id"]) if d.get("tenant_id") else None,
            scope=d.get("scope") or "tenant",
            scope_id=d.get("scope_id"),
            limit_amount=float(d.get("limit_amount") or 0.0),
            warn_ratio=float(d.get("warn_ratio") or 0.8),
            currency=d.get("currency") or "CNY",
            on_exceed=list(on_exceed or DEFAULT_EXCEED_ACTIONS),
            created_at=d.get("created_at") or utcnow(),
        )

    async def get(
        self,
        tenant_id: str | None,
        scope: str,
        scope_id: str | None = None,
    ) -> CostBudgetRecord | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM cost_budgets
                WHERE tenant_id IS NOT DISTINCT FROM $1
                  AND scope = $2
                  AND scope_id IS NOT DISTINCT FROM $3
                """,
                self.scope_key(tenant_id),
                scope,
                scope_id,
            )
        return self._to_record(row) if row else None

    async def upsert(self, budget: CostBudgetRecord) -> CostBudgetRecord:
        """Insert or update by the natural key ``(tenant_id, scope, scope_id)``.

        Deliberately **not** ``ON CONFLICT``: PostgreSQL treats NULLs as
        distinct inside a UNIQUE constraint, and tenant-level budgets have
        ``scope_id IS NULL`` — so ``ON CONFLICT (tenant_id, scope, scope_id)``
        never fires for them and every upsert silently inserted a duplicate
        row. An explicit UPDATE-then-INSERT with ``IS NOT DISTINCT FROM`` is
        NULL-safe and behaves identically on every supported server version.
        """
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            status = await conn.execute(
                """
                UPDATE cost_budgets SET
                    limit_amount = $4,
                    warn_ratio   = $5,
                    currency     = $6,
                    on_exceed    = $7::jsonb
                WHERE tenant_id IS NOT DISTINCT FROM $1
                  AND scope = $2
                  AND scope_id IS NOT DISTINCT FROM $3
                """,
                self.scope_key(budget.tenant_id),
                budget.scope,
                budget.scope_id,
                budget.limit_amount,
                budget.warn_ratio,
                budget.currency,
                json.dumps(list(budget.on_exceed)),
            )
            updated = _affected_rows(status)
            if updated == 0:
                await conn.execute(
                    """
                    INSERT INTO cost_budgets
                      (id, tenant_id, scope, scope_id, limit_amount, warn_ratio,
                       currency, on_exceed, created_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9)
                    """,
                    budget.id,
                    self.scope_key(budget.tenant_id),
                    budget.scope,
                    budget.scope_id,
                    budget.limit_amount,
                    budget.warn_ratio,
                    budget.currency,
                    json.dumps(list(budget.on_exceed)),
                    budget.created_at,
                )
        return budget

    async def list_by_scope(
        self,
        tenant_id: str | None,
        scope: str | None = None,
    ) -> list[CostBudgetRecord]:
        pool = await self._get_pool()
        sql = "SELECT * FROM cost_budgets WHERE tenant_id IS NOT DISTINCT FROM $1"
        args: list[Any] = [self.scope_key(tenant_id)]
        if scope:
            args.append(scope)
            sql += f" AND scope = ${len(args)}"
        sql += " ORDER BY scope, COALESCE(scope_id, ''), created_at DESC"
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [self._to_record(r) for r in rows]

    async def delete(
        self,
        tenant_id: str | None,
        scope: str,
        scope_id: str | None = None,
    ) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            status = await conn.execute(
                """
                DELETE FROM cost_budgets
                WHERE tenant_id IS NOT DISTINCT FROM $1
                  AND scope = $2
                  AND scope_id IS NOT DISTINCT FROM $3
                """,
                self.scope_key(tenant_id),
                scope,
                scope_id,
            )
        # INC40 — count the affected rows, not just "did a DELETE run". The old
        # ``status.upper().startswith("DELETE")`` was truthy even for ``DELETE 0``,
        # so ``DELETE /cost/budgets/{scope}`` would answer 200 for a no-op and
        # mask a cross-tenant / nonexistent delete as success. The Protocol
        # contract is "True iff a row was removed".
        return _affected_rows(status) > 0
