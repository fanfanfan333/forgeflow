"""INC46 T36 (pg-档) — metrics / benchmark tables on real PostgreSQL.

Proves, against the live dev PostgreSQL, what the in-memory store cannot:

* migration ``034`` really created ``metric_snapshots`` / ``benchmark_runs`` with a
  ``NOT NULL`` ``tenant_id`` (红线 5) and ``JSONB`` metrics / report columns;
* a snapshot's **unmeasured** metric survives the round-trip as JSON ``null`` — it
  is never turned into a ``0`` (红线 4), and the values really live in the table;
* benchmark reports (per-category pass matrix) round-trip through a **fresh** store;
* ``latest_*`` selects by the recorded time, not by insertion order;
* the whole surface is tenant-scoped (红线 5): a second tenant reads nothing.

It dials the same dev database the rest of the postgres-profile suite uses and
skips cleanly when it is unreachable (mirroring ``test_inc46_skill_lifecycle_pg.py``).
"""

from __future__ import annotations

import socket
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from forgeflow.config import get_settings
from forgeflow.metrics.store import (
    BenchmarkRunRecord,
    MetricSnapshotRecord,
    MetricsStoreError,
    PostgresMetricsStore,
)

_TENANT_A = f"t-t36-pg-a-{uuid.uuid4().hex[:8]}"
_TENANT_B = f"t-t36-pg-b-{uuid.uuid4().hex[:8]}"
_T0 = datetime(2026, 10, 4, 8, 0, 0, tzinfo=timezone.utc)


def _sync_dsn() -> str:
    return get_settings().postgres_sync_url.replace("postgresql+psycopg://", "postgresql://")


def _async_dsn() -> str:
    return get_settings().postgres_url.replace("postgresql+asyncpg://", "postgresql://")


def _pg_reachable() -> bool:
    dsn = _sync_dsn()
    host, _, rest = dsn.rpartition("@")[2].partition(":")
    port = int(rest.split("/")[0]) if rest.split("/")[0].isdigit() else 5432
    try:
        with socket.create_connection((host or "127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_reachable(), reason="dev PostgreSQL (5433) unreachable"
)


@pytest.fixture()
async def pool():
    asyncpg = pytest.importorskip("asyncpg")
    from forgeflow.database import _init_connection

    created = await asyncpg.create_pool(
        _async_dsn(), min_size=1, max_size=2, command_timeout=15, init=_init_connection
    )
    try:
        yield created
    finally:
        try:
            async with created.acquire() as conn:
                await conn.execute(
                    "DELETE FROM benchmark_runs WHERE tenant_id = ANY($1::text[])",
                    [_TENANT_A, _TENANT_B],
                )
                await conn.execute(
                    "DELETE FROM metric_snapshots WHERE tenant_id = ANY($1::text[])",
                    [_TENANT_A, _TENANT_B],
                )
        except Exception:  # noqa: BLE001 — cleanup must never mask the test result
            pass
        await created.close()


async def test_schema_has_two_tables_not_null_tenant_and_jsonb(pool) -> None:
    """阳性 — 迁移 034 的两表 / NOT NULL tenant / JSONB 列确实存在。"""
    async with pool.acquire() as conn:
        for table in ("metric_snapshots", "benchmark_runs"):
            assert (
                await conn.fetchval(f"SELECT to_regclass('public.{table}')") is not None
            ), table
            nullable = await conn.fetchval(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = $1 AND column_name = 'tenant_id'",
                table,
            )
            assert nullable == "NO", table  # 红线 5

        snap_metrics_type = await conn.fetchval(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'metric_snapshots' AND column_name = 'metrics'"
        )
        run_report_type = await conn.fetchval(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'benchmark_runs' AND column_name = 'report'"
        )
        assert snap_metrics_type == "jsonb"
        assert run_report_type == "jsonb"

        # ``corpus_hash`` carries the frozen fingerprint (varchar 64 = sha256 hex).
        hash_len = await conn.fetchval(
            "SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_name = 'benchmark_runs' AND column_name = 'corpus_hash'"
        )
        assert int(hash_len) == 64


async def test_snapshot_roundtrip_preserves_null_and_tenant_scope(pool) -> None:
    """阳性 — 未测量指标落库后仍是 ``null``（不是 0）；新实例读回；他租户读空。"""
    store = PostgresMetricsStore(_sync_dsn())
    store.add_snapshot(
        MetricSnapshotRecord(
            tenant_id=_TENANT_A,
            metrics={
                "first_pass_success": 0.6222,
                "latency_p95": None,  # unmeasured ⇒ must stay null (红线 4)
                "rollback_rate": 0.0,  # a MEASURED zero — must stay 0
            },
            window_hours=24.0,
            total_tasks=9,
            labeled_runs=9,
            generated_at=_T0,
        )
    )
    fresh = PostgresMetricsStore(_sync_dsn())
    latest = fresh.latest_snapshot(_TENANT_A)
    assert latest is not None
    assert latest.metrics["first_pass_success"] == pytest.approx(0.6222)
    assert latest.metrics["latency_p95"] is None  # null survived, not coerced to 0
    assert latest.metrics["rollback_rate"] == pytest.approx(0.0)
    assert latest.window_hours == pytest.approx(24.0)
    assert latest.total_tasks == 9 and latest.labeled_runs == 9

    # Tenant fail-closed: another tenant sees nothing.
    assert fresh.latest_snapshot(_TENANT_B) is None
    assert fresh.list_snapshots(_TENANT_B) == []


async def test_latest_snapshot_is_chosen_by_generated_at(pool) -> None:
    """阳性 — ``latest_*`` 按记录时间取，而不是插入顺序。"""
    store = PostgresMetricsStore(_sync_dsn())
    store.add_snapshot(
        MetricSnapshotRecord(
            tenant_id=_TENANT_A, metrics={"first_pass_success": 0.9},
            generated_at=_T0 + timedelta(hours=5),
        )
    )
    store.add_snapshot(
        MetricSnapshotRecord(
            tenant_id=_TENANT_A, metrics={"first_pass_success": 0.1},
            generated_at=_T0,  # older, inserted second
        )
    )
    latest = PostgresMetricsStore(_sync_dsn()).latest_snapshot(_TENANT_A)
    assert latest.metrics["first_pass_success"] == pytest.approx(0.9)


async def test_benchmark_run_roundtrip_report_matrix(pool) -> None:
    """阳性 — 基准报告（各类通过矩阵）真落库、可被新实例读回。"""
    store = PostgresMetricsStore(_sync_dsn())
    store.add_benchmark_run(
        BenchmarkRunRecord(
            tenant_id=_TENANT_A,
            corpus_hash="a" * 64,
            total_cases=52,
            passed=52,
            failed=0,
            errors=0,
            skipped=0,
            report={
                "by_category": {
                    "clarify": {"total": 12, "passed": 12},
                    "injection": {"total": 6, "passed": 6},
                },
                "contract_satisfied": True,
            },
            created_at=_T0,
        )
    )
    fresh = PostgresMetricsStore(_sync_dsn())
    latest = fresh.latest_benchmark_run(_TENANT_A)
    assert latest is not None
    assert latest.corpus_hash == "a" * 64
    assert latest.total_cases == 52 and latest.passed == 52
    assert latest.report["by_category"]["clarify"]["passed"] == 12
    assert latest.report["contract_satisfied"] is True

    assert fresh.latest_benchmark_run(_TENANT_B) is None
    assert fresh.list_benchmark_runs(_TENANT_B) == []


async def test_store_rejects_write_without_tenant(pool) -> None:
    """阴性 — 应用层 fail-closed：无租户不写（红线 5）。"""
    store = PostgresMetricsStore(_sync_dsn())
    with pytest.raises(MetricsStoreError):
        store.add_snapshot(MetricSnapshotRecord(tenant_id="", metrics={}))
    with pytest.raises(MetricsStoreError):
        store.add_benchmark_run(BenchmarkRunRecord(tenant_id="", corpus_hash="x"))


async def test_null_tenant_is_rejected_by_the_database(pool) -> None:
    """阴性 — ``tenant_id`` 的 NOT NULL 在数据库层抵御无租户写入（红线 5）。"""
    asyncpg = pytest.importorskip("asyncpg")
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(
                "INSERT INTO metric_snapshots (tenant_id, metrics) "
                "VALUES ($1, '{}'::jsonb)",
                None,
            )
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(
                "INSERT INTO benchmark_runs (tenant_id, corpus_hash) VALUES ($1, $2)",
                None,
                "b" * 64,
            )
