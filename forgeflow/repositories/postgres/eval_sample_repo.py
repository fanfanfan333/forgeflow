"""PostgreSQL ``EvalSampleRepository`` — asyncpg, lazy pool (INC8 §3.2/§4.1-N4).

Talks to the ``agent_eval_samples`` table created by migration ``012``. The
``tenant_id`` column is opaque ``TEXT`` (since migration ``013``): the tenant id
is stored exactly as the application hands it, normalised through ``scope_key``
(``None`` → the ``"default"`` bucket), and reads use ``IS NOT DISTINCT FROM`` so
the literal string ``"default"`` round-trips. There is no UUID coercion.

``run_id`` is ``TEXT`` with **no** foreign key (design §3.2 / §12.2): hub runs
never land in ``workflow_runs``, so a UUID FK (the ``run_metrics`` trap) would
make every judge write fail. A write is best-effort in the sense that it is the
caller's job to decide whether to persist — this repository never silently
swallows a real SQL error.
"""

from __future__ import annotations

import json
from typing import Any

from forgeflow.repositories.base import TenantScopedRepository
from forgeflow.repositories.eval_sample_repo import EvalSample


class PgEvalSampleRepository(TenantScopedRepository):
    """asyncpg-backed ``EvalSampleRepository``."""

    def __init__(self, default_tenant: str = "default", pool: Any | None = None) -> None:
        super().__init__(default_tenant)
        self._pool = pool

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        from forgeflow.database import get_pool

        return await get_pool()

    @staticmethod
    def _to_sample(row: Any) -> EvalSample:
        d = dict(row)
        meta = d.get("meta")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (TypeError, ValueError):
                meta = {}
        return EvalSample(
            id=str(d["id"]) if d.get("id") is not None else "",
            tenant_id=str(d["tenant_id"]) if d.get("tenant_id") else None,
            run_id=str(d["run_id"]) if d.get("run_id") is not None else None,
            metric_name=str(d.get("metric_name") or ""),
            metric_value=float(d.get("metric_value") or 0.0),
            metric_unit=str(d.get("metric_unit") or ""),
            dimension=str(d.get("dimension") or "quality"),
            source=str(d.get("source") or "judge"),
            dataset=d.get("dataset"),
            meta=meta if isinstance(meta, dict) else {},
            created_at=d.get("created_at"),
        )

    async def save_sample(
        self, tenant_id: str | None, sample: EvalSample
    ) -> EvalSample:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agent_eval_samples
                  (id, tenant_id, run_id, metric_name, metric_value, metric_unit,
                   dimension, source, dataset, meta, created_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11)
                """,
                sample.id,
                self.scope_key(
                    tenant_id if tenant_id is not None else sample.tenant_id
                ),
                sample.run_id,
                sample.metric_name,
                sample.metric_value,
                sample.metric_unit or None,
                sample.dimension,
                sample.source,
                sample.dataset,
                json.dumps(sample.meta or {}),
                sample.created_at,
            )
        return sample

    async def list_samples(
        self,
        tenant_id: str | None,
        *,
        metric: str | None = None,
        limit: int = 100,
    ) -> list[EvalSample]:
        pool = await self._get_pool()
        args: list[Any] = [self.scope_key(tenant_id)]
        sql = (
            "SELECT id, tenant_id, run_id, metric_name, metric_value, metric_unit, "
            "dimension, source, dataset, meta, created_at "
            "FROM agent_eval_samples WHERE tenant_id IS NOT DISTINCT FROM $1"
        )
        if metric:
            args.append(metric)
            sql += f" AND metric_name = ${len(args)}"
        args.append(int(limit))
        sql += f" ORDER BY created_at DESC LIMIT ${len(args)}"
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [self._to_sample(r) for r in rows]

    async def aggregate(
        self, tenant_id: str | None, metric: str
    ) -> dict[str, Any]:
        """``{count, avg, min, max, has_data}`` for one metric (SQL side).

        ``has_data`` is derived from ``COUNT(*)`` so an empty cohort reports
        ``False`` with ``None`` statistics — never a fabricated ``0.0``.
        """
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*)        AS count,
                    AVG(metric_value) AS avg,
                    MIN(metric_value) AS min,
                    MAX(metric_value) AS max
                FROM agent_eval_samples
                WHERE tenant_id IS NOT DISTINCT FROM $1
                  AND metric_name = $2
                """,
                self.scope_key(tenant_id),
                metric,
            )
        count = int(row["count"] or 0) if row else 0
        if count == 0:
            return {"count": 0, "avg": None, "min": None, "max": None, "has_data": False}
        return {
            "count": count,
            "avg": float(row["avg"]) if row["avg"] is not None else None,
            "min": float(row["min"]) if row["min"] is not None else None,
            "max": float(row["max"]) if row["max"] is not None else None,
            "has_data": True,
        }


__all__ = ["PgEvalSampleRepository"]
