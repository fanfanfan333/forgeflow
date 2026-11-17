"""Cron schedules (A5 slice 2).

One row per schedule: a 5-field cron expression evaluated in a
named IANA timezone, whose fires enqueue a run request into the
durable queue (:mod:`forgeflow.queue.store`). The scheduler pass
(:func:`fire_due_schedules`, driven by
:class:`forgeflow.queue.worker.QueueWorker`) finds enabled rows
whose ``next_fire_at`` has passed and enqueues one queue row per
fire time.

Exactly-once per fire: the queue row uses the deterministic
idempotency key ``schedule:<schedule_id>:<fire_time_iso>``, so a
double-fire (two workers racing, or a crash between enqueue and
advance) enqueues nothing new — the queue's
``UNIQUE (tenant_id, idempotency_key)`` absorbs it. ``next_fire_at``
advances only when the row still carries the fire time the worker
started from (``advance`` is an expected-value update), so two
workers cannot advance past the same fire.

``next_fire_at`` / ``last_fire_at`` are stored in UTC; ``timezone``
is the IANA name the cron expression is evaluated in (``croniter`` +
``zoneinfo`` — DST transitions are handled by evaluating in local
time and converting back to UTC).

The store is sync by design, mirroring
:mod:`forgeflow.queue.store`; async callers offload with
``asyncio.to_thread``. InMemory is "ephemeral" (tests / no-PG
runs); Postgres is "durable".
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from forgeflow.queue.service import enqueue_task, normalise_task_payload

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_CATCHUP_FIRES",
    "FireReport",
    "InMemoryScheduleStore",
    "PostgresScheduleStore",
    "Schedule",
    "ScheduleStore",
    "create_schedule",
    "fire_due_schedules",
    "get_schedule_store",
    "next_fire",
    "reset_schedule_store",
    "set_schedule_store",
    "update_schedule",
    "validate_cron_expr",
    "validate_timezone",
]

#: Fires enqueued for one schedule in a single pass. A schedule
#: disabled for longer than this many periods converges over
#: successive passes instead of blocking the worker.
MAX_CATCHUP_FIRES = 100


def _utcnow() -> datetime:
    return datetime.now(UTC)


def validate_cron_expr(cron_expr: str) -> str:
    """Validate a 5-field standard cron expression. Raises ValueError.

    ``croniter`` also accepts 6/7-field forms and ``@daily``-style
    shortcuts; this platform's contract is exactly 5 fields, so
    those are rejected here. An expression that can never fire
    (e.g. Feb 31) is rejected too — ``croniter`` raises when it
    cannot find a next date.
    """
    if not isinstance(cron_expr, str) or not cron_expr.strip():
        raise ValueError("cron_expr is required (5-field cron expression)")
    expr = cron_expr.strip()
    if expr.startswith("@"):
        raise ValueError("cron_expr must be a 5-field expression, not a '@' shortcut")
    if len(expr.split()) != 5:
        raise ValueError("cron_expr must have exactly 5 fields (minute hour day month weekday)")
    try:
        from croniter import croniter

        croniter(expr, _utcnow()).get_next(datetime)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 — croniter's error types vary by input
        raise ValueError(f"invalid cron_expr {expr!r}: {exc}") from exc
    return expr


def validate_timezone(timezone: str) -> str:
    """Validate an IANA timezone name. Raises ValueError."""
    if not isinstance(timezone, str) or not timezone.strip():
        raise ValueError("timezone is required (IANA name, e.g. 'UTC')")
    name = timezone.strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone {name!r}") from exc
    return name


def next_fire(cron_expr: str, timezone: str, after: datetime) -> datetime:
    """Next fire after ``after``, returned UTC-aware.

    Evaluated in the schedule's timezone (so DST transitions land
    on the local wall-clock time) and converted back to UTC.
    """
    expr = validate_cron_expr(cron_expr)
    tz = ZoneInfo(validate_timezone(timezone))
    base = after if after.tzinfo is not None else after.replace(tzinfo=UTC)
    from croniter import croniter

    local = base.astimezone(tz)
    nxt = croniter(expr, local).get_next(datetime)
    if nxt.tzinfo is None:  # pragma: no cover — croniter keeps the input tz
        nxt = nxt.replace(tzinfo=tz)
    return nxt.astimezone(UTC)


@dataclass
class Schedule:
    """One cron schedule."""

    id: str
    tenant_id: str
    owner_id: str
    name: str
    cron_expr: str
    timezone: str = "UTC"
    task: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    next_fire_at: datetime | None = None
    last_fire_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        def _iso(value: datetime | None) -> str | None:
            return value.isoformat() if value is not None else None

        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "owner_id": self.owner_id,
            "name": self.name,
            "cron_expr": self.cron_expr,
            "timezone": self.timezone,
            "task": dict(self.task or {}),
            "enabled": bool(self.enabled),
            "next_fire_at": _iso(self.next_fire_at),
            "last_fire_at": _iso(self.last_fire_at),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


class ScheduleStore(Protocol):
    """The schedule contract both implementations satisfy (all sync)."""

    durability: str

    def create(
        self,
        tenant_id: str,
        *,
        owner_id: str,
        name: str,
        cron_expr: str,
        timezone: str,
        task: dict[str, Any],
        enabled: bool,
        next_fire_at: datetime,
    ) -> Schedule: ...

    def get(self, tenant_id: str, schedule_id: str) -> Schedule | None: ...

    def list_schedules(
        self,
        tenant_id: str,
        *,
        limit: int = 20,
        enabled: bool | None = None,
    ) -> list[Schedule]: ...

    def list_due(self, now: datetime, *, limit: int = 100) -> list[Schedule]: ...

    def update(
        self,
        tenant_id: str,
        schedule_id: str,
        *,
        name: str | None = None,
        cron_expr: str | None = None,
        timezone: str | None = None,
        task: dict[str, Any] | None = None,
        enabled: bool | None = None,
        next_fire_at: datetime | None = None,
    ) -> Schedule | None: ...

    def delete(self, tenant_id: str, schedule_id: str) -> bool: ...

    def advance(
        self,
        tenant_id: str,
        schedule_id: str,
        *,
        expected_next_fire_at: datetime,
        next_fire_at: datetime,
        last_fire_at: datetime | None,
    ) -> Schedule | None: ...


def _row_to_schedule(row: dict[str, Any]) -> Schedule:
    """Map a PG row (as a ``{column: value}`` dict) to a Schedule."""
    task = row.get("task")
    if isinstance(task, str):
        task = json.loads(task)
    return Schedule(
        id=str(row.get("id")),
        tenant_id=str(row.get("tenant_id")),
        owner_id=str(row.get("owner_id")),
        name=str(row.get("name") or ""),
        cron_expr=str(row.get("cron_expr") or ""),
        timezone=str(row.get("timezone") or "UTC"),
        task=dict(task or {}),
        enabled=bool(row.get("enabled")),
        next_fire_at=row.get("next_fire_at"),
        last_fire_at=row.get("last_fire_at"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


class InMemoryScheduleStore:
    """Process-lifetime store (tests / no-PG runs)."""

    durability = "ephemeral"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rows: dict[str, Schedule] = {}
        self._seq = 0

    def create(
        self,
        tenant_id: str,
        *,
        owner_id: str,
        name: str,
        cron_expr: str,
        timezone: str,
        task: dict[str, Any],
        enabled: bool,
        next_fire_at: datetime,
    ) -> Schedule:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name is required (non-empty string)")
        expr = validate_cron_expr(cron_expr)
        tz = validate_timezone(timezone)
        now = _utcnow()
        with self._lock:
            self._seq += 1
            row = Schedule(
                id=f"mem-{self._seq}-{uuid.uuid4().hex[:8]}",
                tenant_id=tenant_id,
                owner_id=owner_id,
                name=name.strip(),
                cron_expr=expr,
                timezone=tz,
                task=dict(task or {}),
                enabled=bool(enabled),
                next_fire_at=next_fire_at,
                created_at=now,
                updated_at=now,
            )
            self._rows[row.id] = row
            return row

    def get(self, tenant_id: str, schedule_id: str) -> Schedule | None:
        if not tenant_id or not schedule_id:
            return None
        with self._lock:
            row = self._rows.get(schedule_id)
            if row is None or row.tenant_id != tenant_id:
                return None
            return row

    def list_schedules(
        self,
        tenant_id: str,
        *,
        limit: int = 20,
        enabled: bool | None = None,
    ) -> list[Schedule]:
        if not tenant_id:
            return []
        with self._lock:
            rows = [r for r in self._rows.values() if r.tenant_id == tenant_id]
            if enabled is not None:
                rows = [r for r in rows if r.enabled is bool(enabled)]
            rows.sort(key=lambda r: r.created_at or _utcnow(), reverse=True)
            return rows[: max(1, int(limit))]

    def list_due(self, now: datetime, *, limit: int = 100) -> list[Schedule]:
        with self._lock:
            rows = [
                r
                for r in self._rows.values()
                if r.enabled and r.next_fire_at is not None and r.next_fire_at <= now
            ]
            rows.sort(key=lambda r: r.next_fire_at or now)
            return rows[: max(1, int(limit))]

    def update(
        self,
        tenant_id: str,
        schedule_id: str,
        *,
        name: str | None = None,
        cron_expr: str | None = None,
        timezone: str | None = None,
        task: dict[str, Any] | None = None,
        enabled: bool | None = None,
        next_fire_at: datetime | None = None,
    ) -> Schedule | None:
        with self._lock:
            row = self._rows.get(schedule_id)
            if row is None or row.tenant_id != tenant_id:
                return None
            if name is not None:
                if not isinstance(name, str) or not name.strip():
                    raise ValueError("name is required (non-empty string)")
                row.name = name.strip()
            if cron_expr is not None:
                row.cron_expr = validate_cron_expr(cron_expr)
            if timezone is not None:
                row.timezone = validate_timezone(timezone)
            if task is not None:
                row.task = dict(task)
            if enabled is not None:
                row.enabled = bool(enabled)
            if next_fire_at is not None:
                row.next_fire_at = next_fire_at
            row.updated_at = _utcnow()
            return row

    def delete(self, tenant_id: str, schedule_id: str) -> bool:
        with self._lock:
            row = self._rows.get(schedule_id)
            if row is None or row.tenant_id != tenant_id:
                return False
            del self._rows[schedule_id]
            return True

    def advance(
        self,
        tenant_id: str,
        schedule_id: str,
        *,
        expected_next_fire_at: datetime,
        next_fire_at: datetime,
        last_fire_at: datetime | None,
    ) -> Schedule | None:
        with self._lock:
            row = self._rows.get(schedule_id)
            if row is None or row.tenant_id != tenant_id:
                return None
            # Expected-value update: a worker that already advanced
            # this row (or a newer fire) loses the race and gets None.
            if row.next_fire_at != expected_next_fire_at:
                return None
            row.next_fire_at = next_fire_at
            row.last_fire_at = last_fire_at
            row.updated_at = _utcnow()
            return row


def _normalise_dsn(dsn: str) -> str:
    return dsn.replace("postgresql+psycopg://", "postgresql://")


_COLUMNS = (
    "id, tenant_id, owner_id, name, cron_expr, timezone, task, enabled, "
    "next_fire_at, last_fire_at, created_at, updated_at"
)
_COLUMN_NAMES = _COLUMNS.split(", ")


class PostgresScheduleStore:
    """The real ``schedules`` table (migration ``047``) via ``psycopg``."""

    durability = "durable"

    def __init__(self, dsn: str) -> None:
        self._dsn = _normalise_dsn(dsn)

    def _connect(self):  # noqa: ANN202 — psycopg connection, imported lazily
        import psycopg

        return psycopg.connect(self._dsn)

    def create(
        self,
        tenant_id: str,
        *,
        owner_id: str,
        name: str,
        cron_expr: str,
        timezone: str,
        task: dict[str, Any],
        enabled: bool,
        next_fire_at: datetime,
    ) -> Schedule:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name is required (non-empty string)")
        expr = validate_cron_expr(cron_expr)
        tz = validate_timezone(timezone)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO schedules "
                "(tenant_id, owner_id, name, cron_expr, timezone, task, enabled, next_fire_at) "
                "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s) "
                f"RETURNING {_COLUMNS}",
                (
                    tenant_id,
                    owner_id,
                    name.strip(),
                    expr,
                    tz,
                    json.dumps(task or {}),
                    bool(enabled),
                    next_fire_at,
                ),
            )
            row = cur.fetchone()
            conn.commit()
        if row is None:  # pragma: no cover — an INSERT ... RETURNING always yields a row
            raise RuntimeError("schedules insert returned no row")
        return _row_to_schedule(dict(zip(_COLUMN_NAMES, row, strict=True)))

    def get(self, tenant_id: str, schedule_id: str) -> Schedule | None:
        if not tenant_id or not schedule_id:
            return None
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_COLUMNS} FROM schedules WHERE id = %s AND tenant_id = %s",
                (schedule_id, tenant_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return _row_to_schedule(dict(zip(_COLUMN_NAMES, row, strict=True)))

    def list_schedules(
        self,
        tenant_id: str,
        *,
        limit: int = 20,
        enabled: bool | None = None,
    ) -> list[Schedule]:
        if not tenant_id:
            return []
        with self._connect() as conn, conn.cursor() as cur:
            if enabled is not None:
                cur.execute(
                    f"SELECT {_COLUMNS} FROM schedules "
                    "WHERE tenant_id = %s AND enabled = %s "
                    "ORDER BY created_at DESC LIMIT %s",
                    (tenant_id, bool(enabled), max(1, int(limit))),
                )
            else:
                cur.execute(
                    f"SELECT {_COLUMNS} FROM schedules "
                    "WHERE tenant_id = %s ORDER BY created_at DESC LIMIT %s",
                    (tenant_id, max(1, int(limit))),
                )
            rows = cur.fetchall()
        return [_row_to_schedule(dict(zip(_COLUMN_NAMES, row, strict=True))) for row in rows]

    def list_due(self, now: datetime, *, limit: int = 100) -> list[Schedule]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_COLUMNS} FROM schedules "
                "WHERE enabled AND next_fire_at <= %s "
                "ORDER BY next_fire_at ASC LIMIT %s",
                (now, max(1, int(limit))),
            )
            rows = cur.fetchall()
        return [_row_to_schedule(dict(zip(_COLUMN_NAMES, row, strict=True))) for row in rows]

    def update(
        self,
        tenant_id: str,
        schedule_id: str,
        *,
        name: str | None = None,
        cron_expr: str | None = None,
        timezone: str | None = None,
        task: dict[str, Any] | None = None,
        enabled: bool | None = None,
        next_fire_at: datetime | None = None,
    ) -> Schedule | None:
        assignments: list[str] = []
        params: list[Any] = []
        if name is not None:
            if not isinstance(name, str) or not name.strip():
                raise ValueError("name is required (non-empty string)")
            assignments.append("name = %s")
            params.append(name.strip())
        if cron_expr is not None:
            assignments.append("cron_expr = %s")
            params.append(validate_cron_expr(cron_expr))
        if timezone is not None:
            assignments.append("timezone = %s")
            params.append(validate_timezone(timezone))
        if task is not None:
            assignments.append("task = %s::jsonb")
            params.append(json.dumps(task))
        if enabled is not None:
            assignments.append("enabled = %s")
            params.append(bool(enabled))
        if next_fire_at is not None:
            assignments.append("next_fire_at = %s")
            params.append(next_fire_at)
        if not assignments:
            return self.get(tenant_id, schedule_id)
        assignments.append("updated_at = now()")
        params.extend([schedule_id, tenant_id])
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE schedules SET {', '.join(assignments)} "  # noqa: S608 — assignments are literals
                f"WHERE id = %s AND tenant_id = %s RETURNING {_COLUMNS}",
                params,
            )
            row = cur.fetchone()
            conn.commit()
        if row is None:
            return None
        return _row_to_schedule(dict(zip(_COLUMN_NAMES, row, strict=True)))

    def delete(self, tenant_id: str, schedule_id: str) -> bool:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM schedules WHERE id = %s AND tenant_id = %s RETURNING id",
                (schedule_id, tenant_id),
            )
            deleted = cur.fetchone() is not None
            conn.commit()
        return deleted

    def advance(
        self,
        tenant_id: str,
        schedule_id: str,
        *,
        expected_next_fire_at: datetime,
        next_fire_at: datetime,
        last_fire_at: datetime | None,
    ) -> Schedule | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE schedules SET next_fire_at = %s, last_fire_at = %s, "
                "updated_at = now() WHERE id = %s AND tenant_id = %s "
                f"AND next_fire_at = %s RETURNING {_COLUMNS}",
                (next_fire_at, last_fire_at, schedule_id, tenant_id, expected_next_fire_at),
            )
            row = cur.fetchone()
            conn.commit()
        if row is None:
            return None
        return _row_to_schedule(dict(zip(_COLUMN_NAMES, row, strict=True)))


@dataclass
class FireReport:
    """Counts for one scheduler pass."""

    fired: int = 0
    schedules: int = 0

    def to_dict(self) -> dict[str, int]:
        return {"fired": self.fired, "schedules": self.schedules}


def create_schedule(
    store: ScheduleStore,
    *,
    tenant_id: str,
    owner_id: str,
    name: str,
    cron_expr: str,
    timezone: str = "UTC",
    task: dict[str, Any],
    enabled: bool = True,
    now: datetime | None = None,
) -> Schedule:
    """Validate a schedule and create it with its first ``next_fire_at``.

    The first fire is the next cron time strictly after ``now``.
    """
    payload = normalise_task_payload(task)
    expr = validate_cron_expr(cron_expr)
    tz = validate_timezone(timezone)
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name is required (non-empty string)")
    at = now or _utcnow()
    return store.create(
        tenant_id,
        owner_id=owner_id,
        name=name.strip(),
        cron_expr=expr,
        timezone=tz,
        task=payload,
        enabled=bool(enabled),
        next_fire_at=next_fire(expr, tz, at),
    )


def update_schedule(
    store: ScheduleStore,
    *,
    tenant_id: str,
    schedule_id: str,
    now: datetime | None = None,
    name: str | None = None,
    cron_expr: str | None = None,
    timezone: str | None = None,
    task: dict[str, Any] | None = None,
    enabled: bool | None = None,
) -> Schedule | None:
    """Apply a partial update; recomputes ``next_fire_at`` when the
    cron expression or the timezone changes. Returns None when the
    schedule does not exist under this tenant."""
    current = store.get(tenant_id, schedule_id)
    if current is None:
        return None
    new_cron = cron_expr if cron_expr is not None else current.cron_expr
    new_tz = timezone if timezone is not None else current.timezone
    if cron_expr is not None:
        validate_cron_expr(cron_expr)
    if timezone is not None:
        validate_timezone(timezone)
    payload = normalise_task_payload(task) if task is not None else None
    new_next: datetime | None = None
    if cron_expr is not None or timezone is not None:
        new_next = next_fire(new_cron, new_tz, now or _utcnow())
    return store.update(
        tenant_id,
        schedule_id,
        name=name,
        cron_expr=cron_expr,
        timezone=timezone,
        task=payload,
        enabled=enabled,
        next_fire_at=new_next,
    )


def fire_due_schedules(
    schedule_store: ScheduleStore,
    queue_store: Any,
    *,
    now: datetime | None = None,
) -> FireReport:
    """Fire every due schedule once per due fire time.

    Each fire enqueues one queue row under the deterministic key
    ``schedule:<schedule_id>:<fire_time_iso>`` (a second worker, or
    a re-run after a crash between enqueue and advance, enqueues
    nothing new). The schedule advances only when it still carries
    the fire time this pass started from; a lost race returns None
    from ``advance`` and counts nothing.
    """
    at = now or _utcnow()
    report = FireReport()
    for sched in schedule_store.list_due(at):
        started_from = sched.next_fire_at
        if started_from is None:
            continue
        fire = started_from
        last: datetime | None = sched.last_fire_at
        count = 0
        while fire is not None and fire <= at and count < MAX_CATCHUP_FIRES:
            enqueue_task(
                queue_store,
                tenant_id=sched.tenant_id,
                owner_id=sched.owner_id,
                task=dict(sched.task or {}),
                idempotency_key=f"schedule:{sched.id}:{fire.isoformat()}",
            )
            last = fire
            count += 1
            fire = next_fire(sched.cron_expr, sched.timezone, fire)
        if count == 0:
            continue
        advanced = schedule_store.advance(
            sched.tenant_id,
            sched.id,
            expected_next_fire_at=started_from,
            next_fire_at=fire,
            last_fire_at=last,
        )
        if advanced is not None:
            report.fired += count
            report.schedules += 1
    return report


_STORE: Any = None


def get_schedule_store() -> Any:
    """Process-wide store: PG when a sync DSN is set, else InMemory."""
    global _STORE
    if _STORE is None:
        dsn = os.environ.get("POSTGRES_SYNC_URL") or os.environ.get("POSTGRES_DSN")
        _STORE = PostgresScheduleStore(dsn) if dsn else InMemoryScheduleStore()
    return _STORE


def set_schedule_store(store: Any) -> None:
    global _STORE
    _STORE = store


def reset_schedule_store() -> None:
    global _STORE
    _STORE = None
