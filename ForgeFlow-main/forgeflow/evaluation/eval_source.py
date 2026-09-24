"""INC8 Phase-B — the dual-backend data source behind ``/metrics/agent-eval``.

This is the design's ``N2`` (docs/sop/11-INC8-AGENT-EVALUATION-DESIGN.md §4.1):
it mirrors the existing ``observability/metrics_source.py`` pattern exactly —
one Protocol, a memory implementation that reads the in-process hub run store,
and a PostgreSQL implementation that reads ``workflow_runs`` + the new
``agent_eval_samples`` table, selected by :func:`get_agent_eval_source`.

Honesty contract (design §7)
----------------------------
* The two sources are **never mixed**: the memory source reads *only* the hub
  run store (``source="hub_runs"``, ``durable=False`` — a process-lifetime
  number, exposed not hidden); the PG source reads *only* ``workflow_runs`` /
  ``agent_eval_samples`` (``source="postgres"``, ``durable=True``). Mixing the
  two ``run`` notions would produce a fake number.
* Import safety: the module never imports asyncpg / the runtime at import time;
  those imports are lazy inside the methods, so ``STORAGE_BACKEND=memory`` keeps
  a stdlib-only import graph.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from forgeflow.config import get_settings
from forgeflow.evaluation.agent_metrics import aggregate

logger = logging.getLogger(__name__)

__all__ = [
    "AgentEvalSource",
    "MemoryAgentEvalSource",
    "PostgresAgentEvalSource",
    "get_agent_eval_source",
    "reset_agent_eval_source",
]

#: Upper bound on rows pulled for a read-time aggregate. The run store is small
#: by construction; this is a safety cap, not a paging API.
_MAX_RUNS = 1_000_000
_MAX_SAMPLES = 10_000


@runtime_checkable
class AgentEvalSource(Protocol):
    """The one interface ``/metrics/agent-eval`` reads from."""

    async def summary(self, tenant_id: str | None, window_days: int = 30) -> dict[str, Any]:
        """The three-dimension eval snapshot for one tenant."""
        ...

    async def samples(
        self,
        tenant_id: str | None,
        *,
        metric: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Persisted eval samples (newest first), optionally one metric."""
        ...


class MemoryAgentEvalSource:
    """Aggregates the in-process hub run store (+ memory eval samples).

    ``source="hub_runs"`` / ``durable=False`` are hard-coded because hub runs are
    only ever held in memory (design §7 评审⑪) — a restart genuinely yields "no
    data" (``has_data=False``), never a fabricated zero.
    """

    async def summary(self, tenant_id: str | None, window_days: int = 30) -> dict[str, Any]:
        from forgeflow.repositories.factory import get_eval_sample_repository
        from forgeflow.runtime.orchestrator import get_run_store

        records = list(get_run_store().list(tenant_id, limit=_MAX_RUNS))
        samples: list[Any] = []
        try:
            samples = list(
                await get_eval_sample_repository().list_samples(
                    tenant_id, limit=_MAX_SAMPLES
                )
            )
        except Exception as exc:  # noqa: BLE001 — samples are an enhancement
            logger.warning("agent-eval: memory eval samples unavailable: %s", exc)
        snapshot = aggregate(
            records,
            samples,
            source="hub_runs",
            durable=False,
            window_days=window_days,
            judged_samples=len(samples),
        )
        return snapshot.to_dict()

    async def samples(
        self,
        tenant_id: str | None,
        *,
        metric: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        from forgeflow.repositories.factory import get_eval_sample_repository

        rows = await get_eval_sample_repository().list_samples(
            tenant_id, metric=metric, limit=limit
        )
        return [r.to_dict() for r in rows]


class PostgresAgentEvalSource:
    """Aggregates ``workflow_runs`` (legacy) + ``agent_eval_samples`` (judge).

    Reads are **platform-level**: ``workflow_runs`` has no ``tenant_id`` column
    (design §7 评审⑨), so the run-derived metrics are not tenant-sliced. The
    ``note`` on every metric carries the ``source`` so this is never mistaken for
    a per-tenant figure.

    A real query failure is **not** swallowed here — it propagates so the router
    can report an explicit ``degraded`` payload, matching the A1 fix for
    ``/metrics/evaluation``.
    """

    def __init__(self, pool: Any | None = None) -> None:
        self._pool = pool

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        from forgeflow.database import get_pool

        return await get_pool()

    async def summary(self, tenant_id: str | None, window_days: int = 30) -> dict[str, Any]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            run_rows = await conn.fetch(
                """
                SELECT status, created_at, completed_at, total_tokens, total_cost_usd
                FROM workflow_runs
                WHERE created_at >= now() - make_interval(days => $1)
                ORDER BY created_at DESC
                LIMIT $2
                """,
                int(window_days),
                _MAX_RUNS,
            )
            sample_rows = await conn.fetch(
                """
                SELECT metric_name, metric_value, metric_unit, dimension, source,
                       dataset, created_at
                FROM agent_eval_samples
                ORDER BY created_at DESC
                LIMIT $1
                """,
                _MAX_SAMPLES,
            )
        records = [
            {
                "status": r["status"],
                "created_at": (
                    r["created_at"].isoformat() if r["created_at"] is not None else None
                ),
                "completed_at": (
                    r["completed_at"].isoformat()
                    if r["completed_at"] is not None
                    else None
                ),
                "total_tokens": int(r["total_tokens"] or 0),
                "total_cost_usd": float(r["total_cost_usd"] or 0.0),
            }
            for r in run_rows
        ]
        samples = [
            {
                "metric_name": s["metric_name"],
                "metric_value": s["metric_value"],
            }
            for s in sample_rows
        ]
        snapshot = aggregate(
            records,
            samples,
            source="postgres",
            durable=True,
            window_days=window_days,
            judged_samples=len(samples),
        )
        return snapshot.to_dict()

    async def samples(
        self,
        tenant_id: str | None,
        *,
        metric: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        args: list[Any] = [int(limit)]
        sql = (
            "SELECT metric_name, metric_value, metric_unit, dimension, source, "
            "dataset, created_at FROM agent_eval_samples"
        )
        if metric:
            args.append(metric)
            sql += f" WHERE metric_name = ${len(args)}"
        sql += " ORDER BY created_at DESC LIMIT $1"
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [
            {
                "metric_name": r["metric_name"],
                "metric_value": float(r["metric_value"])
                if r["metric_value"] is not None
                else None,
                "metric_unit": r["metric_unit"],
                "dimension": r["dimension"],
                "source": r["source"],
                "dataset": r["dataset"],
                "created_at": (
                    r["created_at"].isoformat() if r["created_at"] is not None else None
                ),
            }
            for r in rows
        ]


_SOURCE_CACHE: dict[str, AgentEvalSource] = {}


def get_agent_eval_source(*, pool: Any | None = None) -> AgentEvalSource:
    """Return the eval source for the active ``STORAGE_BACKEND`` (cached)."""
    backend = get_settings().storage_backend.lower()
    cached = _SOURCE_CACHE.get(backend)
    if cached is not None and pool is None:
        return cached
    if backend == "memory":
        source: AgentEvalSource = MemoryAgentEvalSource()
    else:
        source = PostgresAgentEvalSource(pool=pool)
    if pool is None:
        _SOURCE_CACHE[backend] = source
    return source


def reset_agent_eval_source() -> None:
    """Drop cached sources — used by tests when switching ``STORAGE_BACKEND``."""
    _SOURCE_CACHE.clear()
