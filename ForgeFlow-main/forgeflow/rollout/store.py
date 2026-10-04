"""INC46 T34 — 灰度/回滚持久层（迁移 032 的三张表，双后端）。

表与语义（任务书 T34 §代码/§DB）
================================
* ``skill_rollouts``   —— 一次灰度发布的**当前状态**（阶段 %、canary/promoted/
  rolled_back）与流量指针（candidate → incumbent）；
* ``rollout_metrics``  —— 每个阶段窗口的**实测**指标快照（未测量列为 NULL，红线 4）；
* ``skill_rollbacks``  —— **只追加**的回滚记录（红线 6 / 20：回滚 = 新增记录 +
  流量指针切回 incumbent；已发布版本可审计，**绝不删除或改写**）。

双后端纪律
----------
* 内存后端服务于离线/单测（``STORAGE_BACKEND=memory`` 或未配 DSN）；
* PG 后端直连 ``POSTGRES_SYNC_URL``（psycopg 同步），与
  :mod:`forgeflow.documents.version_chain` 同一模式；
* ``tenant_id`` 是**每表第一列且 NOT NULL**（红线 5）；未解析租户读写皆空 / 拒写。
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "RolloutError",
    "RolloutRecord",
    "MetricsRecord",
    "RollbackRecord",
    "RolloutStore",
    "InMemoryRolloutStore",
    "PostgresRolloutStore",
    "get_rollout_store",
    "set_rollout_store",
    "reset_rollout_store",
    "STATE_CANARY",
    "STATE_PROMOTED",
    "STATE_ROLLED_BACK",
]

STATE_CANARY = "canary"
STATE_PROMOTED = "promoted"
STATE_ROLLED_BACK = "rolled_back"
_STATES = (STATE_CANARY, STATE_PROMOTED, STATE_ROLLED_BACK)


class RolloutError(ValueError):
    """非法灰度/回滚操作（fail-closed）。"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


@dataclass
class RolloutRecord:
    """一次灰度发布的记录（``skill_rollouts`` 一行）。"""

    tenant_id: str
    skill_id: str
    candidate_version: str
    incumbent_version: str
    stage_pct: int
    state: str = STATE_CANARY
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "skill_id": self.skill_id,
            "candidate_version": self.candidate_version,
            "incumbent_version": self.incumbent_version,
            "stage_pct": self.stage_pct,
            "state": self.state,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


@dataclass
class MetricsRecord:
    """一个阶段窗口的实测指标快照（``rollout_metrics`` 一行）。"""

    tenant_id: str
    rollout_id: str
    stage_pct: int
    labeled_runs: int = 0
    successes: int = 0
    success_rate: float | None = None
    success_rate_lb: float | None = None
    validate_fail_rate: float | None = None
    rework_rate: float | None = None
    p95_latency_ms: float | None = None
    cost: float | None = None
    window_hours: float | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    measured_at: datetime = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "rollout_id": self.rollout_id,
            "stage_pct": self.stage_pct,
            "labeled_runs": self.labeled_runs,
            "successes": self.successes,
            "success_rate": self.success_rate,
            "success_rate_lb": self.success_rate_lb,
            "validate_fail_rate": self.validate_fail_rate,
            "rework_rate": self.rework_rate,
            "p95_latency_ms": self.p95_latency_ms,
            "cost": self.cost,
            "window_hours": self.window_hours,
            "measured_at": _iso(self.measured_at),
        }


@dataclass
class RollbackRecord:
    """一次回滚（``skill_rollbacks`` 一行）—— **只追加**，永不更新/删除。"""

    tenant_id: str
    rollout_id: str
    from_version: str
    to_version: str
    trigger: str
    reason: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: datetime = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "rollout_id": self.rollout_id,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "trigger": self.trigger,
            "reason": self.reason,
            "created_at": _iso(self.created_at),
        }


@runtime_checkable
class RolloutStore(Protocol):
    """灰度/回滚读写面（两后端语义一致；``tenant_id`` 是第一参数）。"""

    def save_rollout(self, record: RolloutRecord) -> RolloutRecord: ...
    def get_rollout(self, tenant_id: str | None, rollout_id: str) -> RolloutRecord | None: ...
    def latest_rollout(self, tenant_id: str | None, skill_id: str) -> RolloutRecord | None: ...
    def list_rollouts(self, tenant_id: str | None, skill_id: str) -> list[RolloutRecord]: ...

    def add_metrics(self, record: MetricsRecord) -> MetricsRecord: ...
    def list_metrics(self, tenant_id: str | None, rollout_id: str) -> list[MetricsRecord]: ...

    def add_rollback(self, record: RollbackRecord) -> RollbackRecord: ...
    def list_rollbacks(self, tenant_id: str | None, rollout_id: str) -> list[RollbackRecord]: ...


# --------------------------------------------------------------------------- #
# In-memory backend                                                            #
# --------------------------------------------------------------------------- #
@dataclass
class _MemoryState:
    rollouts: dict[tuple[str, str], RolloutRecord] = field(default_factory=dict)
    metrics: list[MetricsRecord] = field(default_factory=list)
    rollbacks: list[RollbackRecord] = field(default_factory=list)


class InMemoryRolloutStore:
    """Process-local store (offline profile / unit seam).

    Tenant fail-closed: an unresolved tenant reads empty and refuses writes.
    """

    def __init__(self, state: _MemoryState | None = None) -> None:
        self._state = state if state is not None else _MemoryState()

    def save_rollout(self, record: RolloutRecord) -> RolloutRecord:
        if not record.tenant_id:
            raise RolloutError("拒绝写入：未解析租户（fail-closed，红线 5）")
        if record.state not in _STATES:
            raise RolloutError(f"非法灰度状态：{record.state!r}")
        record.updated_at = _now()
        self._state.rollouts[(record.tenant_id, record.id)] = record
        return record

    def get_rollout(self, tenant_id: str | None, rollout_id: str) -> RolloutRecord | None:
        if not tenant_id:
            return None
        return self._state.rollouts.get((str(tenant_id), str(rollout_id)))

    def latest_rollout(self, tenant_id: str | None, skill_id: str) -> RolloutRecord | None:
        rows = self.list_rollouts(tenant_id, skill_id)
        return rows[-1] if rows else None

    def list_rollouts(self, tenant_id: str | None, skill_id: str) -> list[RolloutRecord]:
        if not tenant_id:
            return []
        rows = [
            r
            for (t, _i), r in self._state.rollouts.items()
            if t == str(tenant_id) and (not skill_id or r.skill_id == skill_id)
        ]
        rows.sort(key=lambda r: (r.created_at, r.id))
        return rows

    def add_metrics(self, record: MetricsRecord) -> MetricsRecord:
        if not record.tenant_id:
            raise RolloutError("拒绝写入：未解析租户（fail-closed，红线 5）")
        self._state.metrics.append(record)
        return record

    def list_metrics(self, tenant_id: str | None, rollout_id: str) -> list[MetricsRecord]:
        if not tenant_id:
            return []
        rows = [
            m
            for m in self._state.metrics
            if m.tenant_id == str(tenant_id) and (not rollout_id or m.rollout_id == rollout_id)
        ]
        rows.sort(key=lambda m: (m.measured_at, m.id))
        return rows

    def add_rollback(self, record: RollbackRecord) -> RollbackRecord:
        if not record.tenant_id:
            raise RolloutError("拒绝写入：未解析租户（fail-closed，红线 5）")
        self._state.rollbacks.append(record)
        return record

    def list_rollbacks(self, tenant_id: str | None, rollout_id: str) -> list[RollbackRecord]:
        if not tenant_id:
            return []
        rows = [
            r
            for r in self._state.rollbacks
            if r.tenant_id == str(tenant_id) and (not rollout_id or r.rollout_id == rollout_id)
        ]
        rows.sort(key=lambda r: (r.created_at, r.id))
        return rows


# --------------------------------------------------------------------------- #
# PostgreSQL backend (migration 032)                                            #
# --------------------------------------------------------------------------- #
def _normalise_dsn(dsn: str) -> str:
    return dsn.replace("postgresql+psycopg://", "postgresql://")


_ROLLOUT_COLS = (
    "id, tenant_id, skill_id, candidate_version, incumbent_version, stage_pct, "
    "state, created_at, updated_at"
)
_METRIC_COLS = (
    "id, tenant_id, rollout_id, stage_pct, labeled_runs, successes, success_rate, "
    "success_rate_lb, validate_fail_rate, rework_rate, p95_latency_ms, cost, "
    "window_hours, measured_at"
)
_ROLLBACK_COLS = (
    "id, tenant_id, rollout_id, from_version, to_version, trigger, reason, created_at"
)


class PostgresRolloutStore:
    """The real ``skill_rollouts`` / ``rollout_metrics`` / ``skill_rollbacks`` tables."""

    def __init__(self, dsn: str) -> None:
        self._dsn = _normalise_dsn(dsn)

    def _connect(self):  # noqa: ANN202 — psycopg connection, imported lazily
        import psycopg

        return psycopg.connect(self._dsn)

    # -- rollouts ---------------------------------------------------------- #
    @staticmethod
    def _rollout_row(row: Any) -> RolloutRecord | None:
        if row is None:
            return None
        names = (
            "id", "tenant_id", "skill_id", "candidate_version", "incumbent_version",
            "stage_pct", "state", "created_at", "updated_at",
        )
        data = dict(zip(names, row))
        return RolloutRecord(
            id=str(data["id"]),
            tenant_id=data["tenant_id"],
            skill_id=data["skill_id"],
            candidate_version=data["candidate_version"],
            incumbent_version=data["incumbent_version"],
            stage_pct=int(data["stage_pct"]),
            state=data["state"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
        )

    def save_rollout(self, record: RolloutRecord) -> RolloutRecord:
        if not record.tenant_id:
            raise RolloutError("拒绝写入：未解析租户（fail-closed，红线 5）")
        if record.state not in _STATES:
            raise RolloutError(f"非法灰度状态：{record.state!r}")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO skill_rollouts
                    (id, tenant_id, skill_id, candidate_version, incumbent_version,
                     stage_pct, state, created_at, updated_at)
                VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (id) DO UPDATE SET
                    stage_pct = EXCLUDED.stage_pct,
                    state = EXCLUDED.state,
                    updated_at = now()
                """,
                (
                    str(record.id),
                    record.tenant_id,
                    record.skill_id,
                    record.candidate_version,
                    record.incumbent_version,
                    int(record.stage_pct),
                    record.state,
                    record.created_at,
                ),
            )
            conn.commit()
        return record

    def get_rollout(self, tenant_id: str | None, rollout_id: str) -> RolloutRecord | None:
        if not tenant_id:
            return None
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_ROLLOUT_COLS} FROM skill_rollouts "
                "WHERE tenant_id = %s AND id = %s::uuid",
                (str(tenant_id), str(rollout_id)),
            )
            return self._rollout_row(cur.fetchone())

    def latest_rollout(self, tenant_id: str | None, skill_id: str) -> RolloutRecord | None:
        if not tenant_id:
            return None
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_ROLLOUT_COLS} FROM skill_rollouts "
                "WHERE tenant_id = %s AND skill_id = %s "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (str(tenant_id), str(skill_id)),
            )
            return self._rollout_row(cur.fetchone())

    def list_rollouts(self, tenant_id: str | None, skill_id: str) -> list[RolloutRecord]:
        if not tenant_id:
            return []
        sql = f"SELECT {_ROLLOUT_COLS} FROM skill_rollouts WHERE tenant_id = %s"
        params: list[Any] = [str(tenant_id)]
        if skill_id:
            sql += " AND skill_id = %s"
            params.append(str(skill_id))
        sql += " ORDER BY created_at, id"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return [self._rollout_row(r) for r in cur.fetchall()]

    # -- metrics ----------------------------------------------------------- #
    @staticmethod
    def _metric_row(row: Any) -> MetricsRecord:
        names = (
            "id", "tenant_id", "rollout_id", "stage_pct", "labeled_runs", "successes",
            "success_rate", "success_rate_lb", "validate_fail_rate", "rework_rate",
            "p95_latency_ms", "cost", "window_hours", "measured_at",
        )
        d = dict(zip(names, row))
        return MetricsRecord(
            id=str(d["id"]),
            tenant_id=d["tenant_id"],
            rollout_id=str(d["rollout_id"]),
            stage_pct=int(d["stage_pct"]),
            labeled_runs=int(d["labeled_runs"]),
            successes=int(d["successes"]),
            success_rate=d["success_rate"],
            success_rate_lb=d["success_rate_lb"],
            validate_fail_rate=d["validate_fail_rate"],
            rework_rate=d["rework_rate"],
            p95_latency_ms=d["p95_latency_ms"],
            cost=d["cost"],
            window_hours=d["window_hours"],
            measured_at=d["measured_at"],
        )

    def add_metrics(self, record: MetricsRecord) -> MetricsRecord:
        if not record.tenant_id:
            raise RolloutError("拒绝写入：未解析租户（fail-closed，红线 5）")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO rollout_metrics
                    (id, tenant_id, rollout_id, stage_pct, labeled_runs, successes,
                     success_rate, success_rate_lb, validate_fail_rate, rework_rate,
                     p95_latency_ms, cost, window_hours, measured_at)
                VALUES (%s::uuid, %s, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(record.id),
                    record.tenant_id,
                    str(record.rollout_id),
                    int(record.stage_pct),
                    int(record.labeled_runs),
                    int(record.successes),
                    record.success_rate,
                    record.success_rate_lb,
                    record.validate_fail_rate,
                    record.rework_rate,
                    record.p95_latency_ms,
                    record.cost,
                    record.window_hours,
                    record.measured_at,
                ),
            )
            conn.commit()
        return record

    def list_metrics(self, tenant_id: str | None, rollout_id: str) -> list[MetricsRecord]:
        if not tenant_id:
            return []
        sql = f"SELECT {_METRIC_COLS} FROM rollout_metrics WHERE tenant_id = %s"
        params: list[Any] = [str(tenant_id)]
        if rollout_id:
            sql += " AND rollout_id = %s::uuid"
            params.append(str(rollout_id))
        sql += " ORDER BY measured_at, id"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return [self._metric_row(r) for r in cur.fetchall()]

    # -- rollbacks --------------------------------------------------------- #
    @staticmethod
    def _rollback_row(row: Any) -> RollbackRecord:
        names = (
            "id", "tenant_id", "rollout_id", "from_version", "to_version",
            "trigger", "reason", "created_at",
        )
        d = dict(zip(names, row))
        return RollbackRecord(
            id=str(d["id"]),
            tenant_id=d["tenant_id"],
            rollout_id=str(d["rollout_id"]),
            from_version=d["from_version"],
            to_version=d["to_version"],
            trigger=d["trigger"],
            reason=d["reason"] or "",
            created_at=d["created_at"],
        )

    def add_rollback(self, record: RollbackRecord) -> RollbackRecord:
        if not record.tenant_id:
            raise RolloutError("拒绝写入：未解析租户（fail-closed，红线 5）")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO skill_rollbacks
                    (id, tenant_id, rollout_id, from_version, to_version, trigger,
                     reason, created_at)
                VALUES (%s::uuid, %s, %s::uuid, %s, %s, %s, %s, %s)
                """,
                (
                    str(record.id),
                    record.tenant_id,
                    str(record.rollout_id),
                    record.from_version,
                    record.to_version,
                    record.trigger,
                    record.reason,
                    record.created_at,
                ),
            )
            conn.commit()
        return record

    def list_rollbacks(self, tenant_id: str | None, rollout_id: str) -> list[RollbackRecord]:
        if not tenant_id:
            return []
        sql = f"SELECT {_ROLLBACK_COLS} FROM skill_rollbacks WHERE tenant_id = %s"
        params: list[Any] = [str(tenant_id)]
        if rollout_id:
            sql += " AND rollout_id = %s::uuid"
            params.append(str(rollout_id))
        sql += " ORDER BY created_at, id"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return [self._rollback_row(r) for r in cur.fetchall()]


# --------------------------------------------------------------------------- #
# Factory                                                                       #
# --------------------------------------------------------------------------- #
_STORE: Any = None


def get_rollout_store() -> RolloutStore:
    """Process-wide store: PG when a DSN is configured, else in-memory."""
    global _STORE
    if _STORE is not None:
        return _STORE
    dsn = os.environ.get("POSTGRES_SYNC_URL") or os.environ.get("POSTGRES_DSN")
    _STORE = PostgresRolloutStore(dsn) if dsn else InMemoryRolloutStore()
    return _STORE


def set_rollout_store(store: RolloutStore) -> None:
    """Test helper — pin an explicit store (usually the in-memory one)."""
    global _STORE
    _STORE = store


def reset_rollout_store() -> None:
    """Test helper — drop the cached store (next call re-resolves it)."""
    global _STORE
    _STORE = None
