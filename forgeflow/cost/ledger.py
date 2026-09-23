"""Cost ledger — the per-run, per-tenant spend source behind ``/cost/*`` (INC2 A1).

Before this module the Cost routes had **no data producer**: ``cost.py``'s
``_period_totals`` was hard-wired to ``(None, 0.0)``, so ``/cost/board`` always
reported ``total_spent = 0`` and ``/cost/savings`` always reported
``has_data = false`` — the home KPI #3「节省成本」could never leave ``—``.

This module turns the recorded run cost into the two numbers the frozen
``/cost`` contract needs:

    ``(previous_period_cost, current_period_cost)``

The baseline rule is the **frozen §7.1 ruling**:
``baseline = previous-period actual cost × Settings.cost_savings_baseline_multiplier``
— i.e. a period-over-period ("上期同口径实际成本") comparison, never a substitute
for the current period's cost.

Honesty rule (hard requirement)
-------------------------------
A baseline only exists when the previous window recorded **billable** cost
(``> 0``). A previous window full of free (mock / self-hosted) runs yields
``previous = None`` → ``/cost/savings`` returns ``amount = null`` and the SPA
renders ``—``. We never emit ``0`` to stand in for "unknown".

Backends
--------
* **memory** — aggregates the in-process hub run store
  (``runtime.orchestrator.get_run_store()``), tenant-scoped and time-windowed.
  This is the ledger the offline profile (``STORAGE_BACKEND=memory``) reads.
* **postgres** — sums ``workflow_runs.total_cost_usd`` over the same windows.
  ``workflow_runs`` carries no ``tenant_id`` column (see migration 010), so the
  PostgreSQL figure is platform-wide, matching
  :class:`~forgeflow.observability.metrics_source.PostgresMetricsSource`, which
  already treats ``workflow_runs`` as global. A tenant-scoped PG ledger is a
  deliberate follow-up (it needs a ``workflow_runs.tenant_id`` column *and* the
  hub run path to persist there); inventing one here would either break the
  frozen schema or fabricate attribution.

Importing this module never opens a connection and never imports asyncpg /
psycopg at module scope (both are imported lazily inside the PG path), so the
offline profile stays stdlib-only.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from forgeflow.config import get_settings

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "period_totals",
    "current_spend",
    "billable",
]

#: Default comparison window (days). Kept in sync with the savings route's
#: ``SAVINGS_WINDOW_DAYS`` so the board and the savings card share one口径.
DEFAULT_WINDOW_DAYS = 30


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp into an aware UTC datetime (or ``None``)."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        # All platform timestamps are UTC; a naive one is treated as such.
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def billable(cost: float | None) -> float:
    """Clamp a recorded cost to a non-negative float (``None`` ⇒ ``0.0``)."""
    try:
        return max(0.0, float(cost or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _memory_period_totals(tenant_id: str | None, window_days: int) -> tuple[float | None, float]:
    """Aggregate the in-process hub run store over the two windows."""
    from forgeflow.runtime.orchestrator import get_run_store

    now = datetime.now(timezone.utc)
    current_start = now - timedelta(days=window_days)
    previous_start = now - timedelta(days=2 * window_days)

    current = 0.0
    previous = 0.0
    previous_billable = False

    records = get_run_store().list(tenant_id, limit=1_000_000)
    for record in records:
        created = _parse_iso(getattr(record, "created_at", None))
        if created is None:
            continue
        cost = billable(getattr(record, "total_cost_usd", 0.0))
        if created >= current_start:
            current += cost
        elif previous_start <= created < current_start:
            previous += cost
            if cost > 0:
                previous_billable = True

    return (previous if previous_billable else None), current


async def _postgres_period_totals(
    tenant_id: str | None, window_days: int, pool: object | None
) -> tuple[float | None, float]:
    """Sum ``workflow_runs.total_cost_usd`` over the two windows (platform-wide)."""
    if pool is None:
        try:
            from forgeflow.database import get_pool

            pool = await get_pool()
        except Exception as exc:  # noqa: BLE001 — a missing pool is "no data", not 500
            logger.warning("cost ledger: PostgreSQL pool unavailable: %s", exc)
            return None, 0.0

    try:
        async with pool.acquire() as conn:  # type: ignore[attr-defined]
            row = await conn.fetchrow(
                """
                SELECT
                    COALESCE(SUM(total_cost_usd) FILTER (
                        WHERE created_at >= now() - make_interval(days => $1)), 0) AS current_cost,
                    COALESCE(SUM(total_cost_usd) FILTER (
                        WHERE created_at >= now() - make_interval(days => $2)
                          AND created_at <  now() - make_interval(days => $1)), 0) AS previous_cost,
                    COUNT(*) FILTER (
                        WHERE created_at >= now() - make_interval(days => $2)
                          AND created_at <  now() - make_interval(days => $1)
                          AND COALESCE(total_cost_usd, 0) > 0) AS previous_billable_runs
                FROM workflow_runs
                """,
                window_days,
                2 * window_days,
            )
    except Exception as exc:  # noqa: BLE001 — degrade, never 500 the dashboard
        logger.warning("cost ledger: workflow_runs aggregate unavailable: %s", exc)
        return None, 0.0

    if row is None:
        return None, 0.0

    current = billable(row["current_cost"])
    previous_billable_runs = int(row["previous_billable_runs"] or 0)
    previous = billable(row["previous_cost"]) if previous_billable_runs > 0 else None
    return previous, current


async def period_totals(
    tenant_id: str | None,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    pool: object | None = None,
) -> tuple[float | None, float]:
    """Return ``(previous_period_cost, current_period_cost)`` for one tenant.

    ``previous_period_cost`` is ``None`` when the previous window recorded no
    billable cost — the caller must then report "no baseline" (``—``), never a
    fabricated ``0``.
    """
    days = int(window_days) if window_days and window_days > 0 else DEFAULT_WINDOW_DAYS
    backend = get_settings().storage_backend.lower()
    if backend == "memory":
        return _memory_period_totals(tenant_id, days)
    return await _postgres_period_totals(tenant_id, days, pool)


async def current_spend(
    tenant_id: str | None,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    pool: object | None = None,
) -> float:
    """Billable spend for ``tenant_id`` over the current window (0.0 when none)."""
    _previous, current = await period_totals(
        tenant_id, window_days=window_days, pool=pool
    )
    return billable(current)
