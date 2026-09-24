"""INC8-A7 — real PostgreSQL write→read-back for the ``run_metrics`` writer.

Proves the production caller chosen for A7 actually lands rows: insert a genuine
``workflow_runs`` row (the FK target that only the native path creates), call
``MetricsStore.record_run_completion``, then **SELECT the rows back** and assert
the values match — and that ``MetricsStore.get_summary`` reflects them.

Skips cleanly when no live Postgres is reachable (same discipline as
``tests/integration/test_agent_eval_pg.py``).
"""

from __future__ import annotations

import json
import socket
import uuid
from decimal import Decimal

import asyncpg
import pytest

from forgeflow.config import get_settings
from forgeflow.observability.metrics_store import MetricsStore

_REAL_GETADDRINFO = socket.getaddrinfo
_DSN = get_settings().postgres_sync_url.replace("postgresql+psycopg://", "postgresql://")

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def pool(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)
    try:
        p = await asyncpg.create_pool(_DSN, min_size=1, max_size=3, timeout=4)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"live Postgres not reachable ({exc})")
    async with p.acquire() as conn:
        if not await conn.fetchval("SELECT to_regclass('public.run_metrics')"):
            await p.close()
            pytest.skip("run_metrics not present on the target DB (migration 003)")
    yield p
    await p.close()


async def test_record_run_completion_write_read_back(pool):
    run_id = str(uuid.uuid4())
    thread_id = str(uuid.uuid4())

    try:
        # The FK target: run_metrics.run_id REFERENCES workflow_runs(id).
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO workflow_runs
                  (id, thread_id, workflow_type, status, input_data, output_data,
                   total_tokens, total_cost_usd, user_id, metadata, workspace_id)
                VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8,$9,$10::jsonb,$11)
                """,
                uuid.UUID(run_id),
                uuid.UUID(thread_id),
                "sales_ops",
                "completed",
                json.dumps({"company_name": "Acme"}),
                json.dumps({"final_stage": "done"}),
                120,
                0.03,
                "admin-1",
                json.dumps({"latency_ms": 812.5}),
                None,
            )

        # The production writer (as wired by the native workflow route).
        await MetricsStore(pool).record_run_completion(
            run_id,
            latency_ms=812.5,
            total_tokens=120,
            total_cost_usd=0.03,
            success=True,
            agent_name="sales_ops",
        )

        # Read the rows straight back.
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT metric_name, metric_value, metric_unit, tags "
                "FROM run_metrics WHERE run_id = $1",
                uuid.UUID(run_id),
            )
        found = {r["metric_name"]: float(r["metric_value"]) for r in rows}
        assert found == {
            "latency_ms": 812.5,
            "tokens_used": 120.0,
            "cost_usd": 0.03,
            "success": 1.0,
        }, found
        # JSONB comes back as a string from asyncpg (no codec registered).
        def _tags(row):
            raw = row["tags"]
            return json.loads(raw) if isinstance(raw, str) else raw

        agent_tags = {_tags(r)["agent"] for r in rows}
        assert agent_tags == {"sales_ops"}

        # The live read path (PostgresMetricsSource → get_summary) now sees it —
        # before A7 this table was permanently empty and total_runs was always 0.
        #
        # ``get_summary`` is a **whole-table, 30-day-window** aggregate, so on a
        # shared dev database its absolute values are not ours to pin: earlier
        # tests/runs leave rows behind (a fixed ``== 812.5`` implicitly assumed the
        # table held only this run — a test-isolation defect, independent of type).
        # We therefore assert only state-independent invariants here and prove the
        # *value* landed via the run-scoped aggregate below.
        #
        # Type trap: ``run_metrics.metric_value`` is ``numeric(18,6)``, so ``AVG``
        # always returns ``decimal.Decimal``. Comparing that to a bare ``float``
        # raises ``TypeError: unsupported operand type(s) for -: 'float' and
        # 'decimal.Decimal'``. Every Decimal comparison here keeps both sides the
        # same type (Decimal vs Decimal).
        summary = await MetricsStore(pool).get_summary()
        assert summary["total_runs"] >= 1
        assert summary["avg_latency_ms"] is not None

        # Run-scoped aggregation — the same ``AVG(...)`` the summary uses, but
        # filtered to *this* run, so the expected value is exact and independent of
        # whatever else the shared table holds. If ``record_run_completion`` were a
        # no-op (A7 unwired) this row would not exist → ``AVG`` returns ``None`` →
        # the assertion below fails, so this is a real wiring proof, not a no-op.
        async with pool.acquire() as conn:
            scoped_avg = await conn.fetchval(
                "SELECT AVG(metric_value) FROM run_metrics "
                "WHERE run_id = $1 AND metric_name = 'latency_ms'",
                uuid.UUID(run_id),
            )
        assert isinstance(scoped_avg, Decimal), f"expected a Decimal, got {scoped_avg!r}"
        assert scoped_avg == Decimal("812.5")

        by_agent = await MetricsStore(pool).get_cost_by_agent(days=7)
        assert any(r["agent"] == "sales_ops" for r in by_agent)
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM run_metrics WHERE run_id = $1", uuid.UUID(run_id))
            await conn.execute("DELETE FROM workflow_runs WHERE id = $1", uuid.UUID(run_id))
