"""INC46 T36 — 指标 / 基准持久层（迁移 034 的两张表，双后端）。

表与语义（任务书 T36 §代码/§DB）
================================
* ``metric_snapshots``  —— 一次指标聚合的**快照**（``metrics`` JSONB；未测量列为
  ``null``，绝不写 0，红线 4）。基线比较读的就是上一张快照。
* ``benchmark_runs``    —— 一次端到端基准运行的**结果**（``corpus_hash`` 冻结基准集
  指纹 + 通过矩阵）。基准集被改动（哈希不符）时 runner **拒绝运行**，不会写行。

双后端纪律
----------
* 内存后端服务于离线 / 单测（``STORAGE_BACKEND=memory`` 或未配 DSN）；
* PG 后端直连 ``POSTGRES_SYNC_URL``（psycopg 同步），与
  :mod:`forgeflow.rollout.store` / :mod:`forgeflow.lifecycle.store` 同一模式；
* ``tenant_id`` 是**每表第一列且 NOT NULL**（红线 5）；未解析租户读写皆空 / 拒写。
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "MetricsStoreError",
    "MetricSnapshotRecord",
    "BenchmarkRunRecord",
    "MetricsStore",
    "InMemoryMetricsStore",
    "PostgresMetricsStore",
    "get_metrics_store",
    "set_metrics_store",
    "reset_metrics_store",
]


class MetricsStoreError(ValueError):
    """非法指标/基准持久化操作（fail-closed）。"""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


@dataclass
class MetricSnapshotRecord:
    """一次指标快照（``metric_snapshots`` 一行）。"""

    tenant_id: str
    metrics: dict[str, Any] = field(default_factory=dict)
    window_hours: float | None = None
    total_tasks: int = 0
    labeled_runs: int = 0
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    generated_at: datetime = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "metrics": dict(self.metrics),
            "window_hours": self.window_hours,
            "total_tasks": self.total_tasks,
            "labeled_runs": self.labeled_runs,
            "generated_at": _iso(self.generated_at),
        }


@dataclass
class BenchmarkRunRecord:
    """一次端到端基准运行（``benchmark_runs`` 一行）。"""

    tenant_id: str
    corpus_hash: str
    total_cases: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    report: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: datetime = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "corpus_hash": self.corpus_hash,
            "total_cases": self.total_cases,
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "skipped": self.skipped,
            "report": dict(self.report),
            "created_at": _iso(self.created_at),
        }


@runtime_checkable
class MetricsStore(Protocol):
    """指标 / 基准读写面（两后端语义一致；``tenant_id`` 是第一参数）。"""

    def add_snapshot(self, record: MetricSnapshotRecord) -> MetricSnapshotRecord: ...
    def latest_snapshot(self, tenant_id: str | None) -> MetricSnapshotRecord | None: ...
    def list_snapshots(self, tenant_id: str | None) -> list[MetricSnapshotRecord]: ...

    def add_benchmark_run(self, record: BenchmarkRunRecord) -> BenchmarkRunRecord: ...
    def latest_benchmark_run(self, tenant_id: str | None) -> BenchmarkRunRecord | None: ...
    def list_benchmark_runs(self, tenant_id: str | None) -> list[BenchmarkRunRecord]: ...


# --------------------------------------------------------------------------- #
# In-memory backend                                                            #
# --------------------------------------------------------------------------- #
@dataclass
class _MemoryState:
    snapshots: list[MetricSnapshotRecord] = field(default_factory=list)
    runs: list[BenchmarkRunRecord] = field(default_factory=list)


class InMemoryMetricsStore:
    """Process-local store (offline profile / unit seam)."""

    def __init__(self, state: _MemoryState | None = None) -> None:
        self._state = state if state is not None else _MemoryState()

    def add_snapshot(self, record: MetricSnapshotRecord) -> MetricSnapshotRecord:
        if not record.tenant_id:
            raise MetricsStoreError("拒绝写入：未解析租户（fail-closed，红线 5）")
        self._state.snapshots.append(record)
        return record

    def latest_snapshot(self, tenant_id: str | None) -> MetricSnapshotRecord | None:
        rows = self.list_snapshots(tenant_id)
        return rows[-1] if rows else None

    def list_snapshots(self, tenant_id: str | None) -> list[MetricSnapshotRecord]:
        if not tenant_id:
            return []
        rows = [s for s in self._state.snapshots if s.tenant_id == str(tenant_id)]
        rows.sort(key=lambda s: (s.generated_at, s.id))
        return rows

    def add_benchmark_run(self, record: BenchmarkRunRecord) -> BenchmarkRunRecord:
        if not record.tenant_id:
            raise MetricsStoreError("拒绝写入：未解析租户（fail-closed，红线 5）")
        self._state.runs.append(record)
        return record

    def latest_benchmark_run(self, tenant_id: str | None) -> BenchmarkRunRecord | None:
        rows = self.list_benchmark_runs(tenant_id)
        return rows[-1] if rows else None

    def list_benchmark_runs(self, tenant_id: str | None) -> list[BenchmarkRunRecord]:
        if not tenant_id:
            return []
        rows = [r for r in self._state.runs if r.tenant_id == str(tenant_id)]
        rows.sort(key=lambda r: (r.created_at, r.id))
        return rows


# --------------------------------------------------------------------------- #
# PostgreSQL backend (migration 034)                                            #
# --------------------------------------------------------------------------- #
def _normalise_dsn(dsn: str) -> str:
    return dsn.replace("postgresql+psycopg://", "postgresql://")


_SNAP_COLS = "id, tenant_id, metrics, window_hours, total_tasks, labeled_runs, generated_at"
_RUN_COLS = (
    "id, tenant_id, corpus_hash, total_cases, passed, failed, errors, skipped, "
    "report, created_at"
)


class PostgresMetricsStore:
    """The real ``metric_snapshots`` / ``benchmark_runs`` tables."""

    def __init__(self, dsn: str) -> None:
        self._dsn = _normalise_dsn(dsn)

    def _connect(self):  # noqa: ANN202 — psycopg connection, imported lazily
        import psycopg

        return psycopg.connect(self._dsn)

    # -- snapshots ---------------------------------------------------------- #
    @staticmethod
    def _snap_row(row: Any) -> MetricSnapshotRecord:
        names = ("id", "tenant_id", "metrics", "window_hours", "total_tasks", "labeled_runs", "generated_at")
        d = dict(zip(names, row))
        raw = d["metrics"]
        if isinstance(raw, str):
            import json

            raw = json.loads(raw)
        return MetricSnapshotRecord(
            id=str(d["id"]),
            tenant_id=d["tenant_id"],
            metrics=dict(raw or {}),
            window_hours=d["window_hours"],
            total_tasks=int(d["total_tasks"] or 0),
            labeled_runs=int(d["labeled_runs"] or 0),
            generated_at=d["generated_at"],
        )

    def add_snapshot(self, record: MetricSnapshotRecord) -> MetricSnapshotRecord:
        if not record.tenant_id:
            raise MetricsStoreError("拒绝写入：未解析租户（fail-closed，红线 5）")
        import json

        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO metric_snapshots
                    (id, tenant_id, metrics, window_hours, total_tasks, labeled_runs, generated_at)
                VALUES (%s::uuid, %s, %s::jsonb, %s, %s, %s, %s)
                """,
                (
                    str(record.id),
                    record.tenant_id,
                    json.dumps(dict(record.metrics)),
                    record.window_hours,
                    int(record.total_tasks),
                    int(record.labeled_runs),
                    record.generated_at,
                ),
            )
            conn.commit()
        return record

    def latest_snapshot(self, tenant_id: str | None) -> MetricSnapshotRecord | None:
        if not tenant_id:
            return None
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_SNAP_COLS} FROM metric_snapshots WHERE tenant_id = %s "
                "ORDER BY generated_at DESC, id DESC LIMIT 1",
                (str(tenant_id),),
            )
            row = cur.fetchone()
            return self._snap_row(row) if row is not None else None

    def list_snapshots(self, tenant_id: str | None) -> list[MetricSnapshotRecord]:
        if not tenant_id:
            return []
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_SNAP_COLS} FROM metric_snapshots WHERE tenant_id = %s "
                "ORDER BY generated_at, id",
                (str(tenant_id),),
            )
            return [self._snap_row(r) for r in cur.fetchall()]

    # -- benchmark runs ----------------------------------------------------- #
    @staticmethod
    def _run_row(row: Any) -> BenchmarkRunRecord:
        names = (
            "id", "tenant_id", "corpus_hash", "total_cases", "passed", "failed",
            "errors", "skipped", "report", "created_at",
        )
        d = dict(zip(names, row))
        raw = d["report"]
        if isinstance(raw, str):
            import json

            raw = json.loads(raw)
        return BenchmarkRunRecord(
            id=str(d["id"]),
            tenant_id=d["tenant_id"],
            corpus_hash=d["corpus_hash"],
            total_cases=int(d["total_cases"] or 0),
            passed=int(d["passed"] or 0),
            failed=int(d["failed"] or 0),
            errors=int(d["errors"] or 0),
            skipped=int(d["skipped"] or 0),
            report=dict(raw or {}),
            created_at=d["created_at"],
        )

    def add_benchmark_run(self, record: BenchmarkRunRecord) -> BenchmarkRunRecord:
        if not record.tenant_id:
            raise MetricsStoreError("拒绝写入：未解析租户（fail-closed，红线 5）")
        import json

        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO benchmark_runs
                    (id, tenant_id, corpus_hash, total_cases, passed, failed, errors,
                     skipped, report, created_at)
                VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    str(record.id),
                    record.tenant_id,
                    record.corpus_hash,
                    int(record.total_cases),
                    int(record.passed),
                    int(record.failed),
                    int(record.errors),
                    int(record.skipped),
                    json.dumps(dict(record.report)),
                    record.created_at,
                ),
            )
            conn.commit()
        return record

    def latest_benchmark_run(self, tenant_id: str | None) -> BenchmarkRunRecord | None:
        if not tenant_id:
            return None
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_RUN_COLS} FROM benchmark_runs WHERE tenant_id = %s "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (str(tenant_id),),
            )
            row = cur.fetchone()
            return self._run_row(row) if row is not None else None

    def list_benchmark_runs(self, tenant_id: str | None) -> list[BenchmarkRunRecord]:
        if not tenant_id:
            return []
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_RUN_COLS} FROM benchmark_runs WHERE tenant_id = %s "
                "ORDER BY created_at, id",
                (str(tenant_id),),
            )
            return [self._run_row(r) for r in cur.fetchall()]


# --------------------------------------------------------------------------- #
# Factory                                                                       #
# --------------------------------------------------------------------------- #
_STORE: Any = None


def get_metrics_store() -> MetricsStore:
    """Process-wide store: PG when a DSN is configured, else in-memory."""
    global _STORE
    if _STORE is not None:
        return _STORE
    dsn = os.environ.get("POSTGRES_SYNC_URL") or os.environ.get("POSTGRES_DSN")
    _STORE = PostgresMetricsStore(dsn) if dsn else InMemoryMetricsStore()
    return _STORE


def set_metrics_store(store: MetricsStore) -> None:
    """Test helper — pin an explicit store (usually the in-memory one)."""
    global _STORE
    _STORE = store


def reset_metrics_store() -> None:
    """Test helper — drop the cached store (next call re-resolves it)."""
    global _STORE
    _STORE = None
