"""Durable task queue store (A5 slice 1).

One row per enqueued run request. The store is sync by design,
mirroring :mod:`forgeflow.runtime.checkpoints` and the pending-action
store; async callers offload with ``asyncio.to_thread``.

State machine (a row is always in exactly one status)::

    queued ──claim──▶ claimed ──mark_running──▶ running ──complete──▶ done
       ▲                  │                         │
       │                  └──── lease expires ──────┘
       │                       (reap → queued)
       └──── fail (attempts < max) ────▶ failed (retry at next_attempt_at)
       └──── fail (attempts >= max) ───▶ dead

``claim`` picks rows in ``queued`` or ``failed`` whose
``next_attempt_at`` has passed, oldest-due first. ``fail`` keeps the
row in ``failed`` with a backoff ``next_attempt_at`` so the retry is
visible in the list API instead of silently re-queued; ``dead`` is
terminal (a human re-enqueues with a new idempotency key).

Exactly-once: ``enqueue`` is idempotent on ``(tenant_id,
idempotency_key)`` — a re-delivered enqueue returns the existing row.
``claim`` is atomic (PG: one UPDATE over a SKIP LOCKED subquery;
InMemory: under the store lock) and hands the row to one worker
under a lease; ``mark_running`` / ``complete`` / ``fail`` all verify
``claimed_by`` + a live lease, so a worker whose lease expired
cannot complete a row another worker has since claimed.

Durability tiers (Phase 9 B9): InMemory is "ephemeral" (tests /
no-PG runs — rows die with the process); Postgres is "durable".
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

logger = logging.getLogger(__name__)

__all__ = [
    "CLAIMABLE_STATUSES",
    "DEFAULT_BACKOFF_BASE_SECONDS",
    "DEFAULT_BACKOFF_CAP_SECONDS",
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "InMemoryTaskQueueStore",
    "PostgresTaskQueueStore",
    "QUEUED_STATUSES",
    "QueuedTask",
    "TaskQueueStore",
    "backoff_delay",
    "get_task_queue_store",
    "reset_task_queue_store",
    "set_task_queue_store",
]

#: Statuses a claim may pick up (due rows awaiting a first run or a retry).
CLAIMABLE_STATUSES: tuple[str, ...] = ("queued", "failed")
#: Every status a row may carry (mirrors the migration CHECK).
QUEUED_STATUSES: tuple[str, ...] = ("queued", "claimed", "running", "done", "failed", "dead")
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_LEASE_SECONDS = 120
DEFAULT_BACKOFF_BASE_SECONDS = 30
DEFAULT_BACKOFF_CAP_SECONDS = 3600


def backoff_delay(
    attempts: int,
    *,
    base_seconds: int = DEFAULT_BACKOFF_BASE_SECONDS,
    cap_seconds: int = DEFAULT_BACKOFF_CAP_SECONDS,
) -> timedelta:
    """Retry delay after ``attempts`` failures: base * 2**(n-1), capped.

    ``attempts`` is the count AFTER incrementing (first failure → 1 →
    base seconds). Pure, so both stores and the tests share it.
    """
    exponent = max(0, int(attempts) - 1)
    return timedelta(seconds=min(int(base_seconds) * (2**exponent), int(cap_seconds)))


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class QueuedTask:
    """One enqueued run request."""

    id: str
    tenant_id: str
    owner_id: str
    task: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str = ""
    status: str = "queued"
    attempts: int = 0
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    next_attempt_at: datetime | None = None
    claimed_by: str | None = None
    lease_expires_at: datetime | None = None
    last_error: str | None = None
    run_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        def _iso(value: datetime | None) -> str | None:
            return value.isoformat() if value is not None else None

        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "owner_id": self.owner_id,
            "task": dict(self.task or {}),
            "idempotency_key": self.idempotency_key,
            "status": self.status,
            "attempts": int(self.attempts),
            "max_attempts": int(self.max_attempts),
            "next_attempt_at": _iso(self.next_attempt_at),
            "claimed_by": self.claimed_by,
            "lease_expires_at": _iso(self.lease_expires_at),
            "last_error": self.last_error,
            "run_id": self.run_id,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


class TaskQueueStore(Protocol):
    """The queue contract both implementations satisfy (all sync)."""

    durability: str

    def enqueue(
        self,
        tenant_id: str,
        *,
        owner_id: str,
        task: dict[str, Any],
        idempotency_key: str,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> QueuedTask: ...

    def get(self, tenant_id: str, task_id: str) -> QueuedTask | None: ...

    def list_tasks(
        self,
        tenant_id: str,
        *,
        limit: int = 20,
        status: str | None = None,
    ) -> list[QueuedTask]: ...

    def claim(
        self,
        worker_id: str,
        *,
        limit: int = 5,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> list[QueuedTask]: ...

    def mark_running(self, tenant_id: str, task_id: str, worker_id: str) -> QueuedTask | None: ...

    def complete(
        self, tenant_id: str, task_id: str, worker_id: str, run_id: str
    ) -> QueuedTask | None: ...

    def fail(
        self,
        tenant_id: str,
        task_id: str,
        worker_id: str,
        error: str,
        *,
        now: datetime | None = None,
    ) -> QueuedTask | None: ...

    def reap_expired_leases(self, *, now: datetime | None = None) -> int: ...


def _row_to_task(row: dict[str, Any]) -> QueuedTask:
    """Map a PG row (as a ``{column: value}`` dict) to a QueuedTask.

    Callers build the dict with ``dict(zip(_COLUMN_NAMES, row,
    strict=True))`` so the column order is the SELECT order.
    """
    task = row.get("task")
    if isinstance(task, str):
        task = json.loads(task)
    return QueuedTask(
        id=str(row.get("id")),
        tenant_id=str(row.get("tenant_id")),
        owner_id=str(row.get("owner_id")),
        task=dict(task or {}),
        idempotency_key=str(row.get("idempotency_key") or ""),
        status=str(row.get("status")),
        attempts=int(row.get("attempts") or 0),
        max_attempts=int(row.get("max_attempts") or DEFAULT_MAX_ATTEMPTS),
        next_attempt_at=row.get("next_attempt_at"),
        claimed_by=row.get("claimed_by"),
        lease_expires_at=row.get("lease_expires_at"),
        last_error=row.get("last_error"),
        run_id=row.get("run_id"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


class InMemoryTaskQueueStore:
    """Process-lifetime store (tests / no-PG runs)."""

    durability = "ephemeral"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rows: dict[str, QueuedTask] = {}
        self._by_key: dict[tuple[str, str], str] = {}
        self._seq = 0

    def enqueue(
        self,
        tenant_id: str,
        *,
        owner_id: str,
        task: dict[str, Any],
        idempotency_key: str,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> QueuedTask:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        now = _utcnow()
        with self._lock:
            existing_id = self._by_key.get((tenant_id, idempotency_key))
            if existing_id is not None:
                existing = self._rows.get(existing_id)
                if existing is not None:
                    return existing
            self._seq += 1
            row = QueuedTask(
                id=f"mem-{self._seq}-{uuid.uuid4().hex[:8]}",
                tenant_id=tenant_id,
                owner_id=owner_id,
                task=dict(task or {}),
                idempotency_key=idempotency_key,
                status="queued",
                attempts=0,
                max_attempts=max(1, int(max_attempts)),
                next_attempt_at=now,
                created_at=now,
                updated_at=now,
            )
            self._rows[row.id] = row
            self._by_key[(tenant_id, idempotency_key)] = row.id
            return row

    def get(self, tenant_id: str, task_id: str) -> QueuedTask | None:
        if not tenant_id or not task_id:
            return None
        with self._lock:
            row = self._rows.get(task_id)
            if row is None or row.tenant_id != tenant_id:
                return None
            return row

    def list_tasks(
        self,
        tenant_id: str,
        *,
        limit: int = 20,
        status: str | None = None,
    ) -> list[QueuedTask]:
        if not tenant_id:
            return []
        with self._lock:
            rows = [r for r in self._rows.values() if r.tenant_id == tenant_id]
            if status is not None:
                rows = [r for r in rows if r.status == status]
            rows.sort(key=lambda r: r.created_at or _utcnow(), reverse=True)
            return rows[: max(1, int(limit))]

    def claim(
        self,
        worker_id: str,
        *,
        limit: int = 5,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> list[QueuedTask]:
        if not worker_id:
            raise ValueError("worker_id is required")
        at = now or _utcnow()
        lease_until = at + timedelta(seconds=max(1, int(lease_seconds)))
        with self._lock:
            due = [
                r
                for r in self._rows.values()
                if r.status in CLAIMABLE_STATUSES
                and (r.next_attempt_at is None or r.next_attempt_at <= at)
            ]
            due.sort(key=lambda r: (r.next_attempt_at or at, r.created_at or at))
            picked = due[: max(1, int(limit))]
            for row in picked:
                row.status = "claimed"
                row.claimed_by = worker_id
                row.lease_expires_at = lease_until
                row.updated_at = at
            return list(picked)

    def _leased_row(
        self, tenant_id: str, task_id: str, worker_id: str, *, now: datetime | None = None
    ) -> QueuedTask | None:
        """The row iff this worker still holds a live lease on it."""
        row = self._rows.get(task_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        if row.claimed_by != worker_id:
            return None
        at = now or _utcnow()
        if row.lease_expires_at is not None and row.lease_expires_at <= at:
            return None
        if row.status not in ("claimed", "running"):
            return None
        return row

    def mark_running(self, tenant_id: str, task_id: str, worker_id: str) -> QueuedTask | None:
        with self._lock:
            row = self._leased_row(tenant_id, task_id, worker_id)
            if row is None:
                return None
            row.status = "running"
            row.updated_at = _utcnow()
            return row

    def complete(
        self, tenant_id: str, task_id: str, worker_id: str, run_id: str
    ) -> QueuedTask | None:
        with self._lock:
            row = self._leased_row(tenant_id, task_id, worker_id)
            if row is None:
                return None
            row.status = "done"
            row.run_id = run_id
            row.claimed_by = None
            row.lease_expires_at = None
            row.updated_at = _utcnow()
            return row

    def fail(
        self,
        tenant_id: str,
        task_id: str,
        worker_id: str,
        error: str,
        *,
        now: datetime | None = None,
    ) -> QueuedTask | None:
        at = now or _utcnow()
        with self._lock:
            row = self._leased_row(tenant_id, task_id, worker_id, now=at)
            if row is None:
                return None
            row.attempts += 1
            row.last_error = str(error)[:2000]
            row.claimed_by = None
            row.lease_expires_at = None
            row.updated_at = at
            if row.attempts >= row.max_attempts:
                row.status = "dead"
            else:
                row.status = "failed"
                row.next_attempt_at = at + backoff_delay(row.attempts)
            return row

    def reap_expired_leases(self, *, now: datetime | None = None) -> int:
        at = now or _utcnow()
        with self._lock:
            reaped = 0
            for row in self._rows.values():
                if row.status not in ("claimed", "running"):
                    continue
                if row.lease_expires_at is None or row.lease_expires_at > at:
                    continue
                row.status = "queued"
                row.claimed_by = None
                row.lease_expires_at = None
                row.updated_at = at
                reaped += 1
            return reaped


def _normalise_dsn(dsn: str) -> str:
    return dsn.replace("postgresql+psycopg://", "postgresql://")


_COLUMNS = (
    "id, tenant_id, owner_id, task, idempotency_key, status, attempts, "
    "max_attempts, next_attempt_at, claimed_by, lease_expires_at, "
    "last_error, run_id, created_at, updated_at"
)
_COLUMN_NAMES = _COLUMNS.split(", ")


class PostgresTaskQueueStore:
    """The real ``task_queue`` table (migration ``046``) via ``psycopg``.

    Phase 9 B9 durability tier "durable": rows survive process
    restarts, so a worker crash mid-run is recovered by lease reap
    instead of losing the request.
    """

    durability = "durable"

    def __init__(self, dsn: str) -> None:
        self._dsn = _normalise_dsn(dsn)

    def _connect(self):  # noqa: ANN202 — psycopg connection, imported lazily
        import psycopg

        return psycopg.connect(self._dsn)

    def enqueue(
        self,
        tenant_id: str,
        *,
        owner_id: str,
        task: dict[str, Any],
        idempotency_key: str,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> QueuedTask:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO task_queue "
                "(tenant_id, owner_id, task, idempotency_key, max_attempts) "
                "VALUES (%s, %s, %s::jsonb, %s, %s) "
                "ON CONFLICT (tenant_id, idempotency_key) DO NOTHING "
                f"RETURNING {_COLUMNS}",
                (
                    tenant_id,
                    owner_id,
                    json.dumps(task or {}),
                    idempotency_key,
                    max(1, int(max_attempts)),
                ),
            )
            row = cur.fetchone()
            if row is None:
                # Idempotent replay: the row already exists — return it.
                cur.execute(
                    f"SELECT {_COLUMNS} FROM task_queue "
                    "WHERE tenant_id = %s AND idempotency_key = %s",
                    (tenant_id, idempotency_key),
                )
                row = cur.fetchone()
            conn.commit()
        if row is None:  # pragma: no cover — conflict + missing is impossible
            raise RuntimeError("task_queue enqueue conflict with no existing row")
        return _row_to_task(dict(zip(_COLUMN_NAMES, row, strict=True)))

    def get(self, tenant_id: str, task_id: str) -> QueuedTask | None:
        if not tenant_id or not task_id:
            return None
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_COLUMNS} FROM task_queue WHERE id = %s AND tenant_id = %s",
                (task_id, tenant_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return _row_to_task(dict(zip(_COLUMN_NAMES, row, strict=True)))

    def list_tasks(
        self,
        tenant_id: str,
        *,
        limit: int = 20,
        status: str | None = None,
    ) -> list[QueuedTask]:
        if not tenant_id:
            return []
        with self._connect() as conn, conn.cursor() as cur:
            if status is not None:
                cur.execute(
                    f"SELECT {_COLUMNS} FROM task_queue "
                    "WHERE tenant_id = %s AND status = %s "
                    "ORDER BY created_at DESC LIMIT %s",
                    (tenant_id, status, max(1, int(limit))),
                )
            else:
                cur.execute(
                    f"SELECT {_COLUMNS} FROM task_queue "
                    "WHERE tenant_id = %s ORDER BY created_at DESC LIMIT %s",
                    (tenant_id, max(1, int(limit))),
                )
            rows = cur.fetchall()
        return [_row_to_task(dict(zip(_COLUMN_NAMES, row, strict=True))) for row in rows]

    def claim(
        self,
        worker_id: str,
        *,
        limit: int = 5,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> list[QueuedTask]:
        if not worker_id:
            raise ValueError("worker_id is required")
        # One statement: the SKIP LOCKED subquery locks the due rows
        # and the outer UPDATE claims exactly those rows — no
        # check-then-claim window between two workers.
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE task_queue SET status = 'claimed', claimed_by = %s, "
                "lease_expires_at = now() + (%s || ' seconds')::interval, "
                "updated_at = now() "
                "WHERE id IN ("
                "  SELECT id FROM task_queue "
                "  WHERE status IN ('queued', 'failed') AND next_attempt_at <= now() "
                "  ORDER BY next_attempt_at ASC, created_at ASC LIMIT %s "
                "  FOR UPDATE SKIP LOCKED"
                ") "
                f"RETURNING {_COLUMNS}",
                (worker_id, str(max(1, int(lease_seconds))), max(1, int(limit))),
            )
            rows = cur.fetchall()
            conn.commit()
        return [_row_to_task(dict(zip(_COLUMN_NAMES, row, strict=True))) for row in rows]

    def _leased_row_id(self, cur: Any, tenant_id: str, task_id: str, worker_id: str) -> bool:
        """True iff this worker holds a live lease on the row (row-locked)."""
        cur.execute(
            "SELECT 1 FROM task_queue WHERE id = %s AND tenant_id = %s "
            "AND claimed_by = %s AND status IN ('claimed', 'running') "
            "AND lease_expires_at > now() FOR UPDATE",
            (task_id, tenant_id, worker_id),
        )
        return cur.fetchone() is not None

    def mark_running(self, tenant_id: str, task_id: str, worker_id: str) -> QueuedTask | None:
        with self._connect() as conn, conn.cursor() as cur:
            if not self._leased_row_id(cur, tenant_id, task_id, worker_id):
                conn.commit()
                return None
            cur.execute(
                "UPDATE task_queue SET status = 'running', updated_at = now() "
                "WHERE id = %s AND tenant_id = %s "
                f"RETURNING {_COLUMNS}",
                (task_id, tenant_id),
            )
            row = cur.fetchone()
            conn.commit()
        return _row_to_task(dict(zip(_COLUMN_NAMES, row, strict=True))) if row else None

    def complete(
        self, tenant_id: str, task_id: str, worker_id: str, run_id: str
    ) -> QueuedTask | None:
        with self._connect() as conn, conn.cursor() as cur:
            if not self._leased_row_id(cur, tenant_id, task_id, worker_id):
                conn.commit()
                return None
            cur.execute(
                "UPDATE task_queue SET status = 'done', run_id = %s, "
                "claimed_by = NULL, lease_expires_at = NULL, updated_at = now() "
                "WHERE id = %s AND tenant_id = %s "
                f"RETURNING {_COLUMNS}",
                (run_id, task_id, tenant_id),
            )
            row = cur.fetchone()
            conn.commit()
        return _row_to_task(dict(zip(_COLUMN_NAMES, row, strict=True))) if row else None

    def fail(
        self,
        tenant_id: str,
        task_id: str,
        worker_id: str,
        error: str,
        *,
        now: datetime | None = None,
    ) -> QueuedTask | None:
        at = now or _utcnow()
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT attempts, max_attempts FROM task_queue "
                "WHERE id = %s AND tenant_id = %s AND claimed_by = %s "
                "AND status IN ('claimed', 'running') "
                "AND lease_expires_at > now() FOR UPDATE",
                (task_id, tenant_id, worker_id),
            )
            found = cur.fetchone()
            if found is None:
                conn.commit()
                return None
            attempts = int(found[0]) + 1
            max_attempts = int(found[1])
            if attempts >= max_attempts:
                cur.execute(
                    "UPDATE task_queue SET status = 'dead', attempts = %s, "
                    "last_error = %s, claimed_by = NULL, lease_expires_at = NULL, "
                    "updated_at = now() WHERE id = %s AND tenant_id = %s "
                    f"RETURNING {_COLUMNS}",
                    (attempts, str(error)[:2000], task_id, tenant_id),
                )
            else:
                retry_at = at + backoff_delay(attempts)
                cur.execute(
                    "UPDATE task_queue SET status = 'failed', attempts = %s, "
                    "last_error = %s, next_attempt_at = %s, "
                    "claimed_by = NULL, lease_expires_at = NULL, "
                    "updated_at = now() WHERE id = %s AND tenant_id = %s "
                    f"RETURNING {_COLUMNS}",
                    (attempts, str(error)[:2000], retry_at, task_id, tenant_id),
                )
            row = cur.fetchone()
            conn.commit()
        return _row_to_task(dict(zip(_COLUMN_NAMES, row, strict=True))) if row else None

    def reap_expired_leases(self, *, now: datetime | None = None) -> int:
        # The comparison uses DB now() (single clock for all workers);
        # ``now`` is accepted for signature parity with the InMemory
        # store and is ignored here.
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE task_queue SET status = 'queued', claimed_by = NULL, "
                "lease_expires_at = NULL, updated_at = now() "
                "WHERE status IN ('claimed', 'running') "
                "AND lease_expires_at IS NOT NULL AND lease_expires_at <= now()"
            )
            reaped = cur.rowcount
            conn.commit()
        return int(reaped)


_STORE: Any = None


def get_task_queue_store() -> Any:
    """Process-wide store: PG when a sync DSN is set, else InMemory."""
    global _STORE
    if _STORE is None:
        dsn = os.environ.get("POSTGRES_SYNC_URL") or os.environ.get("POSTGRES_DSN")
        _STORE = PostgresTaskQueueStore(dsn) if dsn else InMemoryTaskQueueStore()
    return _STORE


def set_task_queue_store(store: Any) -> None:
    global _STORE
    _STORE = store


def reset_task_queue_store() -> None:
    global _STORE
    _STORE = None
