"""Worker heartbeats (A5 slice 4).

One row per worker process in the ``worker_heartbeats`` table
(migration ``049``), upserted once per poll interval by
:class:`forgeflow.queue.worker.QueueWorker` (and once at startup).
Operators read liveness from ``GET /queue/workers``: a heartbeat
whose ``last_beat_at`` is older than 3x the worker's poll interval
is stale — the worker is down.

The poll interval is recorded in ``last_report`` (``poll_seconds``)
because the API has no other way to know a worker's interval; the
staleness rule is ``now - last_beat_at > 3 * poll_seconds`` (the
default interval is 5s, so the default staleness horizon is 15s).

The heartbeat write is best-effort by design: the worker catches
and logs a failed beat and never lets it fail a pass (see
:func:`forgeflow.queue.worker.QueueWorker._beat`).

The store is sync by design, mirroring
:mod:`forgeflow.queue.store`; async callers offload with
``asyncio.to_thread``. InMemory is "ephemeral" (tests / no-PG
runs); Postgres is "durable".
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

logger = logging.getLogger(__name__)

__all__ = [
    "STALE_AFTER_MULTIPLIER",
    "DEFAULT_POLL_SECONDS",
    "InMemoryHeartbeatStore",
    "PostgresHeartbeatStore",
    "WorkerHeartbeat",
    "HeartbeatStore",
    "default_hostname",
    "get_heartbeat_store",
    "reset_heartbeat_store",
    "set_heartbeat_store",
]

#: A worker is stale when it has not beaten for this many poll
#: intervals. The multiplier (not a fixed window) keeps the rule
#: meaningful for both the 5s default and slower deployments.
STALE_AFTER_MULTIPLIER = 3

#: Poll interval assumed when a heartbeat carries none in
#: ``last_report`` (heartbeats written before this module, or by a
#: hand-written row) — the Settings default.
DEFAULT_POLL_SECONDS = 5


def _utcnow() -> datetime:
    return datetime.now(UTC)


def default_hostname() -> str:
    """Best-effort hostname for a heartbeat row."""
    try:
        return socket.gethostname()
    except Exception:  # noqa: BLE001 — a hostname is nice-to-have, never fatal
        return "unknown"


@dataclass
class WorkerHeartbeat:
    """One worker process's latest heartbeat."""

    worker_id: str
    hostname: str | None = None
    started_at: datetime | None = None
    last_beat_at: datetime | None = None
    last_report: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime | None = None

    def poll_seconds(self) -> int:
        """The worker's poll interval, from ``last_report``."""
        try:
            return max(1, int(self.last_report.get("poll_seconds", DEFAULT_POLL_SECONDS)))
        except (TypeError, ValueError):
            return DEFAULT_POLL_SECONDS

    def is_stale(self, now: datetime | None = None) -> bool:
        """True when no beat arrived within 3 poll intervals."""
        if self.last_beat_at is None:
            return True
        at = now or _utcnow()
        beat = self.last_beat_at
        if beat.tzinfo is None:
            beat = beat.replace(tzinfo=UTC)
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        return (at - beat) > timedelta(seconds=STALE_AFTER_MULTIPLIER * self.poll_seconds())

    def to_dict(self) -> dict[str, Any]:
        def _iso(value: datetime | None) -> str | None:
            return value.isoformat() if value is not None else None

        return {
            "worker_id": self.worker_id,
            "hostname": self.hostname,
            "started_at": _iso(self.started_at),
            "last_beat_at": _iso(self.last_beat_at),
            "last_report": dict(self.last_report or {}),
            "updated_at": _iso(self.updated_at),
        }


class HeartbeatStore(Protocol):
    """The heartbeat contract both implementations satisfy (all sync)."""

    durability: str

    def beat(
        self,
        worker_id: str,
        *,
        hostname: str | None,
        started_at: datetime,
        last_report: dict[str, Any],
        now: datetime | None = None,
    ) -> WorkerHeartbeat: ...

    def get(self, worker_id: str) -> WorkerHeartbeat | None: ...

    def list_heartbeats(self, *, limit: int = 20) -> list[WorkerHeartbeat]: ...


def _row_to_heartbeat(row: dict[str, Any]) -> WorkerHeartbeat:
    """Map a PG row (as a ``{column: value}`` dict) to a WorkerHeartbeat."""
    report = row.get("last_report")
    if isinstance(report, str):
        report = json.loads(report)
    return WorkerHeartbeat(
        worker_id=str(row.get("worker_id")),
        hostname=row.get("hostname"),
        started_at=row.get("started_at"),
        last_beat_at=row.get("last_beat_at"),
        last_report=dict(report or {}),
        updated_at=row.get("updated_at"),
    )


class InMemoryHeartbeatStore:
    """Process-lifetime store (tests / no-PG runs)."""

    durability = "ephemeral"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rows: dict[str, WorkerHeartbeat] = {}

    def beat(
        self,
        worker_id: str,
        *,
        hostname: str | None,
        started_at: datetime,
        last_report: dict[str, Any],
        now: datetime | None = None,
    ) -> WorkerHeartbeat:
        if not worker_id:
            raise ValueError("worker_id is required")
        at = now or _utcnow()
        with self._lock:
            row = self._rows.get(worker_id)
            if row is None:
                row = WorkerHeartbeat(
                    worker_id=worker_id,
                    hostname=hostname,
                    started_at=started_at,
                    last_beat_at=at,
                    last_report=dict(last_report or {}),
                    updated_at=at,
                )
                self._rows[worker_id] = row
            else:
                # started_at is the process start: never overwritten
                # by a later beat from the same worker id.
                row.hostname = hostname if hostname is not None else row.hostname
                row.last_beat_at = at
                row.last_report = dict(last_report or {})
                row.updated_at = at
            return row

    def get(self, worker_id: str) -> WorkerHeartbeat | None:
        if not worker_id:
            return None
        with self._lock:
            return self._rows.get(worker_id)

    def list_heartbeats(self, *, limit: int = 20) -> list[WorkerHeartbeat]:
        with self._lock:
            rows = sorted(
                self._rows.values(),
                key=lambda r: r.last_beat_at or _utcnow(),
                reverse=True,
            )
            return rows[: max(1, int(limit))]


def _normalise_dsn(dsn: str) -> str:
    return dsn.replace("postgresql+psycopg://", "postgresql://")


_COLUMNS = "worker_id, hostname, started_at, last_beat_at, last_report, updated_at"
_COLUMN_NAMES = _COLUMNS.split(", ")


class PostgresHeartbeatStore:
    """The real ``worker_heartbeats`` table (migration ``049``) via ``psycopg``."""

    durability = "durable"

    def __init__(self, dsn: str) -> None:
        self._dsn = _normalise_dsn(dsn)

    def _connect(self):  # noqa: ANN202 — psycopg connection, imported lazily
        import psycopg

        return psycopg.connect(self._dsn)

    def beat(
        self,
        worker_id: str,
        *,
        hostname: str | None,
        started_at: datetime,
        last_report: dict[str, Any],
        now: datetime | None = None,
    ) -> WorkerHeartbeat:
        if not worker_id:
            raise ValueError("worker_id is required")
        at = now or _utcnow()
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO worker_heartbeats "
                "(worker_id, hostname, started_at, last_beat_at, last_report) "
                "VALUES (%s, %s, %s, %s, %s::jsonb) "
                "ON CONFLICT (worker_id) DO UPDATE SET "
                "hostname = EXCLUDED.hostname, "
                "last_beat_at = EXCLUDED.last_beat_at, "
                "last_report = EXCLUDED.last_report, "
                "updated_at = now() "
                f"RETURNING {_COLUMNS}",
                (
                    worker_id,
                    hostname,
                    started_at,
                    at,
                    json.dumps(last_report or {}),
                ),
            )
            row = cur.fetchone()
            conn.commit()
        if row is None:  # pragma: no cover — an INSERT ... RETURNING always yields a row
            raise RuntimeError("worker_heartbeats upsert returned no row")
        return _row_to_heartbeat(dict(zip(_COLUMN_NAMES, row, strict=True)))

    def get(self, worker_id: str) -> WorkerHeartbeat | None:
        if not worker_id:
            return None
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_COLUMNS} FROM worker_heartbeats WHERE worker_id = %s",
                (worker_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return _row_to_heartbeat(dict(zip(_COLUMN_NAMES, row, strict=True)))

    def list_heartbeats(self, *, limit: int = 20) -> list[WorkerHeartbeat]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_COLUMNS} FROM worker_heartbeats ORDER BY last_beat_at DESC LIMIT %s",
                (max(1, int(limit)),),
            )
            rows = cur.fetchall()
        return [_row_to_heartbeat(dict(zip(_COLUMN_NAMES, row, strict=True))) for row in rows]


_STORE: Any = None


def get_heartbeat_store() -> Any:
    """Process-wide store: PG when a sync DSN is set, else InMemory."""
    global _STORE
    if _STORE is None:
        dsn = os.environ.get("POSTGRES_SYNC_URL") or os.environ.get("POSTGRES_DSN")
        _STORE = PostgresHeartbeatStore(dsn) if dsn else InMemoryHeartbeatStore()
    return _STORE


def set_heartbeat_store(store: Any) -> None:
    global _STORE
    _STORE = store


def reset_heartbeat_store() -> None:
    global _STORE
    _STORE = None
