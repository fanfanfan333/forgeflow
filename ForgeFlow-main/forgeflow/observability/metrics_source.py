"""Metrics source — the single data provider behind ``/metrics/*`` (INC2-06 / D2).

Before this module the dashboard aggregation lived **inline** in
``api/routers/metrics.py`` as ``_summarize_hub_runs()``. That left the memory
backend with no data path at all (the router always assumed PostgreSQL), which
is exactly gap D2: memory runs produced empty KPIs.

This module extracts the aggregation into a backend-selected source:

  * ``MemoryMetricsSource``   — aggregates the in-process hub run store
    (``runtime.orchestrator.get_run_store()``). Fully offline, stdlib only.
  * ``PostgresMetricsSource`` — delegates to the pre-existing ``MetricsStore``
    plus the original ``workflow_runs`` query. **Behaviour is intentionally
    unchanged** (regression protection): same fields, same SQL, same ordering.

Both expose the same Protocol, so ``/metrics/`` and ``/metrics/runs`` route
through ``get_metrics_source()`` instead of branching in the router.

Honesty flags
-------------
Every derived field carries a companion ``has_*`` flag so the SPA renders ``—``
instead of a fabricated ``0.0%`` / ``0``:

  * ``has_data``          — no runs at all
  * ``has_cost``          — this path records no billable cost
  * ``has_success_rate``  — no *terminal* runs ⇒ a success rate is meaningless
  * ``has_latency``       — no completed_at ⇒ no duration to average

Importing this module never opens a connection and never imports asyncpg /
psycopg at module scope (they are imported lazily inside the PG methods), so
``STORAGE_BACKEND=memory`` runs with stdlib only.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from forgeflow.config import get_settings

logger = logging.getLogger(__name__)

__all__ = [
    "MetricsSource",
    "MemoryMetricsSource",
    "PostgresMetricsSource",
    "get_metrics_source",
    "reset_metrics_source",
]

_TERMINAL_STATUSES = ("completed", "failed")


def _duration_ms(created_at: str | None, completed_at: str | None) -> float | None:
    """Wall-clock run duration in milliseconds, or ``None`` when unavailable."""
    if not created_at or not completed_at:
        return None
    try:
        start = datetime.fromisoformat(created_at)
        end = datetime.fromisoformat(completed_at)
    except (TypeError, ValueError):
        return None
    return max(0.0, (end - start).total_seconds() * 1000.0)


def _run_cost(record: Any) -> float:
    """The billable USD cost recorded on a run, clamped to a non-negative float."""
    try:
        return max(0.0, float(getattr(record, "total_cost_usd", 0.0) or 0.0))
    except (TypeError, ValueError):
        return 0.0


@runtime_checkable
class MetricsSource(Protocol):
    """The one interface ``/metrics/*`` reads from."""

    async def summary(self, tenant_id: str | None) -> dict[str, Any]:
        """Aggregated KPI payload for one tenant."""
        ...

    async def recent_runs(
        self,
        tenant_id: str | None,
        limit: int = 20,
        *,
        workspace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Most recent runs, newest first. ``workspace_id`` only affects PG."""
        ...


class MemoryMetricsSource:
    """Aggregates the AgentFlow hub run store — the live ``POST /tasks`` path.

    This is the source of truth for the home-dashboard KPIs in the offline
    profile. Cost is derived from each run's recorded ``total_cost_usd`` (the
    A1 producer in ``runtime/orchestrator.py``): the deterministic platform
    graph reports no billable usage, so ``has_cost`` stays ``False`` — an
    honest "no billable spend", never a fabricated ``$0.00`` — until a run
    actually records a priced call. ``has_cost`` is therefore inferred from the
    data, not hard-coded.
    """

    async def summary(self, tenant_id: str | None) -> dict[str, Any]:
        from forgeflow.runtime.orchestrator import get_run_store

        records = get_run_store().list(tenant_id, limit=1_000_000)
        total = len(records)
        terminal = [r for r in records if r.status in _TERMINAL_STATUSES]
        completed = sum(1 for r in terminal if r.status == "completed")
        latencies = [
            d for r in records if (d := _duration_ms(r.created_at, r.completed_at)) is not None
        ]
        # Cost comes straight from the run records (A1 producer). A run with a
        # billable (priced) call makes has_cost True; free/mock runs leave it
        # False so the SPA keeps rendering 「—」.
        total_cost_usd = sum(_run_cost(r) for r in records)
        cost_runs = sum(1 for r in records if _run_cost(r) > 0)
        return {
            "total_runs": total,
            "terminal_runs": len(terminal),
            "success_rate": (completed / len(terminal)) if terminal else 0.0,
            "avg_latency_ms": (sum(latencies) / len(latencies)) if latencies else 0.0,
            "avg_cost_usd": (total_cost_usd / total) if total else 0.0,
            "total_cost_usd": total_cost_usd,
            "has_data": total > 0,
            # Derived from real data: True only when a run recorded billable
            # (priced) cost. Never hard-coded.
            "has_cost": cost_runs > 0,
            # A success rate needs terminal runs; an average latency needs at
            # least one completed_at. Both are meaningless on an empty cohort.
            "has_success_rate": bool(terminal),
            "has_latency": bool(latencies),
            "source": "hub_runs",
        }

    async def recent_runs(
        self,
        tenant_id: str | None,
        limit: int = 20,
        *,
        workspace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        from forgeflow.runtime.orchestrator import get_run_store

        records = list(get_run_store().list(tenant_id, limit=1_000_000))
        # Newest first — created_at is an ISO-8601 string, so lexicographic
        # order matches chronological order for our always-UTC timestamps.
        records.sort(key=lambda r: r.created_at or "", reverse=True)
        return [
            {
                "run_id": r.run_id,
                "thread_id": r.thread_id,
                "workflow_type": "agentflow_task",
                "status": r.status,
                "created_at": r.created_at,
                "completed_at": r.completed_at,
                "total_tokens": int(getattr(r, "total_tokens", 0) or 0),
                "total_cost_usd": _run_cost(r),
            }
            for r in records[:limit]
        ]


class PostgresMetricsSource:
    """Delegates to ``MetricsStore`` + the original ``workflow_runs`` queries.

    Kept deliberately conservative: the SQL, field mapping and ordering below
    mirror what ``api/routers/metrics.py`` did before the extraction, so the
    PostgreSQL profile's ``/metrics/*`` output is unchanged.
    """

    def __init__(self, pool: Any | None = None) -> None:
        self._pool = pool

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        from forgeflow.database import get_pool

        return await get_pool()

    async def summary(self, tenant_id: str | None) -> dict[str, Any]:
        # Lazily imported: metrics_store imports asyncpg at module scope, which
        # must stay out of the offline (memory) profile's import graph.
        from forgeflow.observability.metrics_store import MetricsStore

        pool = await self._get_pool()
        # 1) The legacy aggregate (run_metrics) — unchanged, and globally
        #    scoped: workflow_runs has no tenant_id column, so there is
        #    nothing safe to filter on without changing prior behaviour.
        store = MetricsStore(pool)
        aggregate = await store.get_summary()
        total_runs = int(aggregate.get("total_runs") or 0)
        success_rate = float(aggregate.get("success_rate") or 0.0)
        avg_latency_ms = float(aggregate.get("avg_latency_ms") or 0.0)
        total_cost_usd = float(aggregate.get("total_cost_usd") or 0.0)
        avg_cost_usd = float(aggregate.get("avg_cost_usd") or 0.0)

        # 2) Terminal/cost counts come straight from workflow_runs.
        terminal_runs = 0
        cost_runs = 0
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE status IN ('completed', 'failed')) AS terminal_runs,
                        COUNT(*) FILTER (WHERE COALESCE(total_cost_usd, 0) > 0)   AS cost_runs
                    FROM workflow_runs
                    """
                )
            if row:
                terminal_runs = int(row["terminal_runs"] or 0)
                cost_runs = int(row["cost_runs"] or 0)
        except Exception as exc:  # noqa: BLE001 — degrade, never 500 the dashboard
            logger.warning("metrics summary: workflow_runs counts unavailable: %s", exc)

        return {
            "total_runs": total_runs,
            "terminal_runs": terminal_runs,
            "success_rate": success_rate,
            "avg_latency_ms": avg_latency_ms,
            "avg_cost_usd": avg_cost_usd,
            "total_cost_usd": total_cost_usd,
            "has_data": total_runs > 0,
            # Unlike the hub path, the legacy /workflows/run path DOES record
            # cost, so cost is meaningful as soon as a priced run exists.
            "has_cost": cost_runs > 0,
            "has_success_rate": terminal_runs > 0,
            "has_latency": avg_latency_ms > 0,
            "source": "postgres",
        }

    async def recent_runs(
        self,
        tenant_id: str | None,
        limit: int = 20,
        *,
        workspace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        import uuid as _uuid

        pool = await self._get_pool()
        async with pool.acquire() as conn:
            if workspace_id:
                rows = await conn.fetch(
                    """
                    SELECT id, thread_id, workflow_type, status, created_at, completed_at,
                           total_tokens, total_cost_usd, metadata
                    FROM workflow_runs
                    WHERE workspace_id = $1
                    ORDER BY created_at DESC
                    LIMIT $2
                    """,
                    _uuid.UUID(workspace_id),
                    limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, thread_id, workflow_type, status, created_at, completed_at,
                           total_tokens, total_cost_usd, metadata
                    FROM workflow_runs
                    WHERE workspace_id IS NULL
                    ORDER BY created_at DESC
                    LIMIT $1
                    """,
                    limit,
                )
        return [
            {
                "run_id": str(r["id"]),
                "thread_id": str(r["thread_id"]),
                "workflow_type": r["workflow_type"],
                "status": r["status"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
                "total_tokens": r["total_tokens"],
                "total_cost_usd": float(r["total_cost_usd"]),
            }
            for r in rows
        ]


_SOURCE_CACHE: dict[str, MetricsSource] = {}


def get_metrics_source(*, pool: Any | None = None) -> MetricsSource:
    """Return the metrics source for the active ``STORAGE_BACKEND``.

    Cached per backend so the in-memory aggregates are computed against the same
    instance the router already used; ``reset_metrics_source()`` drops it.
    """
    backend = get_settings().storage_backend.lower()
    cached = _SOURCE_CACHE.get(backend)
    if cached is not None and pool is None:
        return cached
    if backend == "memory":
        source: MetricsSource = MemoryMetricsSource()
    else:
        source = PostgresMetricsSource(pool=pool)
    if pool is None:
        _SOURCE_CACHE[backend] = source
    return source


def reset_metrics_source() -> None:
    """Drop cached sources — used by tests when switching STORAGE_BACKEND."""
    _SOURCE_CACHE.clear()
