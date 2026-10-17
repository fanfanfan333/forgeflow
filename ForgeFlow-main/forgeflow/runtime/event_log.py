"""Persistable run-event log — cross-instance / cross-restart event history.

Why this module exists
-----------------------
``RunEventBus`` keeps its history in process memory (a bounded ring). A restart,
or a *different* instance serving the same run, therefore cannot replay the run's
events. This module adds a durable, **append-only** per-run event log behind a
tiny protocol so the bus can *best-effort* persist every emitted event and the
SSE outlet can replay from a cursor (``Last-Event-ID``).

Design (mirrors ``forgeflow.hitl.pending`` — protocol + backends + factory):

* :class:`RunEventLog` — the protocol (``append`` / ``read`` / ``max_seq``);
* :class:`InProcessEventLog` — process-local dict (offline tests + the
  acceptance scenarios); de-duplicates on ``(run_id, seq)`` exactly like the
  Postgres primary key would;
* :class:`PostgresRunEventLog` — the real ``run_events`` table (migration
  ``038``) via :mod:`psycopg`, with ``ON CONFLICT (run_id, sequence) DO NOTHING``
  for idempotency and a ``delete_before`` retention sweep;
* :func:`get_event_log` / :func:`set_event_log` / :func:`reset_event_log` — the
  process-wide factory.

Persistence is **best-effort**: the bus never lets a logging failure fail a run.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from forgeflow.config import get_settings

logger = logging.getLogger(__name__)

#: The keys every :meth:`RunEventLog.read` row carries (ascending ``seq``).
_ROW_KEYS: tuple[str, ...] = ("run_id", "seq", "type", "data", "ts")


def _now() -> datetime:
    return datetime.now(UTC)


def _normalise_dsn(dsn: str) -> str:
    """Strip the SQLAlchemy driver suffix so ``psycopg`` can parse the DSN."""
    return dsn.replace("postgresql+psycopg://", "postgresql://")


def _coerce_payload(value: Any) -> dict[str, Any]:
    """Decode a JSONB column that may arrive as ``dict`` or JSON text."""
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(decoded) if isinstance(decoded, dict) else {}
    if isinstance(value, dict):
        return dict(value)
    return {}


def _public_row(row: dict[str, Any]) -> dict[str, Any]:
    """Project an internal row onto the documented public shape (5 keys)."""
    ts = row.get("ts")
    if isinstance(ts, datetime):
        ts = ts.isoformat()
    return {
        "run_id": str(row.get("run_id", "")),
        "seq": int(row.get("seq", 0)),
        "type": str(row.get("type", "")),
        "data": dict(row.get("data") or {}),
        "ts": str(ts) if ts else _now().isoformat(),
    }


@runtime_checkable
class RunEventLog(Protocol):
    """Append-only, per-run, sequence-ordered event log."""

    def append(self, run_id: str, seq: int, event_type: str, payload: dict[str, Any]) -> None:
        """Persist one event. Idempotent on ``(run_id, seq)``."""
        ...

    def read(self, run_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        """Return rows with ``seq > after_seq`` in ascending order.

        Each row is ``{"run_id", "seq", "type", "data", "ts"}``.
        """
        ...

    def max_seq(self, run_id: str) -> int:
        """Highest persisted ``seq`` for ``run_id`` (``0`` when none)."""
        ...


class InProcessEventLog:
    """Process-local event log (offline profile + acceptance simulations).

    Deliberately mirrors the Postgres semantics exactly — append is idempotent on
    ``(run_id, seq)`` and ``read`` is ascending — so the offline unit suite can
    prove the bus wiring with no database.
    """

    def __init__(self) -> None:
        self._rows: dict[str, list[dict[str, Any]]] = {}

    def append(self, run_id: str, seq: int, event_type: str, payload: dict[str, Any]) -> None:
        rows = self._rows.setdefault(run_id, [])
        seq = int(seq)
        # ON CONFLICT (run_id, sequence) DO NOTHING — a duplicate seq is a no-op.
        if any(r["seq"] == seq for r in rows):
            return
        rows.append(
            {
                "run_id": run_id,
                "seq": seq,
                "type": event_type,
                "data": dict(payload or {}),
                "ts": _now().isoformat(),
                "created_at": _now(),
            }
        )
        rows.sort(key=lambda r: r["seq"])

    def read(self, run_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        return [
            _public_row(r)
            for r in self._rows.get(run_id, [])
            if r["seq"] > after_seq
        ]

    def max_seq(self, run_id: str) -> int:
        rows = self._rows.get(run_id)
        if not rows:
            return 0
        return max(int(r["seq"]) for r in rows)

    def delete_before(self, cutoff: datetime) -> int:
        """Delete rows created strictly before ``cutoff``; return the count.

        Extension method (not part of the protocol) for the retention sweep.
        """
        removed = 0
        for run_id, rows in list(self._rows.items()):
            keep = [r for r in rows if r["created_at"] >= cutoff]
            removed += len(rows) - len(keep)
            if keep:
                self._rows[run_id] = keep
            else:
                self._rows.pop(run_id, None)
        return removed


class PostgresRunEventLog:
    """The real ``run_events`` table (migration ``038``) via ``psycopg``.

    Synchronous by design, mirroring
    :class:`forgeflow.hitl.pending.PostgresPendingActionStore`: the async bus
    calls it directly (best-effort), and the acceptance suite constructs it with
    an explicit DSN so it runs without the async pool.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = _normalise_dsn(dsn)

    def _connect(self, tenant_id: str | None = None):  # noqa: ANN202 — psycopg conn, lazy
        import psycopg

        from forgeflow.tenant_scope import current_tenant

        conn = psycopg.connect(self._dsn)
        conn.execute(
            "SELECT set_config('app.tenant_id', %s, true)",
            (tenant_id or current_tenant() or "",),
        )
        return conn

    def append(self, run_id: str, seq: int, event_type: str, payload: dict[str, Any]) -> None:
        try:
            from forgeflow.tenant_scope import current_tenant

            tenant_id = current_tenant()
        except Exception:  # noqa: BLE001 — no ambient tenant ⇒ NULL (nullable column)
            tenant_id = None
        with self._connect(tenant_id) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO run_events
                    (run_id, sequence, event_type, payload, tenant_id, created_at)
                VALUES (%s, %s, %s, %s::jsonb, %s, %s)
                ON CONFLICT (run_id, sequence) DO NOTHING
                """,
                (
                    str(run_id),
                    int(seq),
                    str(event_type),
                    json.dumps(payload or {}),
                    str(tenant_id) if tenant_id else None,
                    _now(),
                ),
            )
            conn.commit()

    def read(self, run_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT run_id, sequence, event_type, payload, created_at "
                "FROM run_events WHERE run_id = %s AND sequence > %s "
                "ORDER BY sequence ASC",
                (str(run_id), int(after_seq)),
            )
            rows = cur.fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            run_id_v, seq_v, type_v, payload_v, created_v = row
            created = created_v if isinstance(created_v, datetime) else _now()
            result.append(
                _public_row(
                    {
                        "run_id": run_id_v,
                        "seq": seq_v,
                        "type": type_v,
                        "data": _coerce_payload(payload_v),
                        "ts": created.isoformat(),
                    }
                )
            )
        return result

    def max_seq(self, run_id: str) -> int:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM run_events WHERE run_id = %s",
                (str(run_id),),
            )
            row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def delete_before(self, cutoff: datetime) -> int:
        """Delete rows created strictly before ``cutoff``; return the count.

        Retention extension (not part of the protocol): only ``run_events`` is
        touched — the ``workspace_runs`` projection is never deleted here.
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM run_events WHERE created_at < %s", (cutoff,))
            removed = cur.rowcount or 0
            conn.commit()
        return int(removed)


def sweep_run_events(
    log: RunEventLog, retention_days: int, now: datetime | None = None
) -> int:
    """Delete events older than ``retention_days``; return rows removed.

    A no-op when ``retention_days <= 0`` (cleanup disabled) or the backend has no
    ``delete_before``. Deliberately **callable only** — this round schedules no
    background job (avoids colliding with the OUT-of-scope worker-cluster work).
    """
    if retention_days <= 0:
        return 0
    delete_before = getattr(log, "delete_before", None)
    if delete_before is None:
        return 0
    cutoff = (now or _now()) - timedelta(days=retention_days)
    return int(delete_before(cutoff))


# --------------------------------------------------------------------------- #
# Factory                                                                      #
# --------------------------------------------------------------------------- #
_LOG: RunEventLog | None = None
_LOG_RESOLVED = False


def _build_event_log() -> RunEventLog | None:
    """Resolve the log: Postgres when configured, else ``None`` (no persistence).

    Alignment with ``get_pending_store``: a configured ``POSTGRES_SYNC_URL`` /
    ``POSTGRES_DSN`` selects the Postgres backend. It is additionally gated on a
    non-memory ``storage_backend`` so the offline profile
    (``storage_backend=memory``) is byte-for-byte unchanged even when a DSN
    happens to be present in the environment (e.g. the test conftest default).
    """
    settings = get_settings()
    if settings.storage_backend.lower() == "memory":
        return None
    dsn = os.environ.get("POSTGRES_SYNC_URL") or os.environ.get("POSTGRES_DSN")
    if dsn:
        return PostgresRunEventLog(dsn)
    return None


def get_event_log() -> RunEventLog | None:
    """Process-wide event log, or ``None`` when persistence is not configured."""
    global _LOG, _LOG_RESOLVED
    if not _LOG_RESOLVED:
        _LOG = _build_event_log()
        _LOG_RESOLVED = True
        if _LOG is not None:
            logger.info("run event log: Postgres (durable)")
        else:
            logger.info("run event log: none (in-memory bus history only)")
    return _LOG


def set_event_log(log: RunEventLog | None) -> None:
    """Pin an explicit log (or ``None``) — tests / acceptance simulations."""
    global _LOG, _LOG_RESOLVED
    _LOG = log
    _LOG_RESOLVED = True


def reset_event_log() -> None:
    """Drop the cached log so the next call re-resolves it from config."""
    global _LOG, _LOG_RESOLVED
    _LOG = None
    _LOG_RESOLVED = False


__all__ = [
    "InProcessEventLog",
    "PostgresRunEventLog",
    "RunEventLog",
    "get_event_log",
    "reset_event_log",
    "set_event_log",
    "sweep_run_events",
]
