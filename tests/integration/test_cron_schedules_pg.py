"""A5 slice 2: Postgres schedule store + firing against the real
``schedules`` / ``task_queue`` tables.

Skips when live Postgres is unreachable or migration 047 has not
been applied (the migration's own application is verified by the
alembic upgrade in the verification run, not by this test).
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from forgeflow.queue.schedules import (
    PostgresScheduleStore,
    create_schedule,
    fire_due_schedules,
    next_fire,
)
from forgeflow.queue.store import PostgresTaskQueueStore

_TENANT = "t-cron-pg"
_OTHER = "t-cron-pg-other"


def _dsn():
    dsn = os.environ.get("POSTGRES_SYNC_URL") or os.environ.get("POSTGRES_DSN") or ""
    return dsn.replace("postgresql+psycopg://", "postgresql://")


@pytest.fixture
def stores():
    dsn = _dsn()
    if not dsn:
        pytest.skip("no sync Postgres DSN")
    try:
        conn = psycopg.connect(dsn)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"live Postgres not reachable ({exc})")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM schedules LIMIT 1")
            cur.execute("SELECT 1 FROM task_queue LIMIT 1")
    except Exception:  # noqa: BLE001
        conn.close()
        pytest.skip("schedules/task_queue table missing (migrations 046/047 not applied)")
    conn.close()
    sched = PostgresScheduleStore(dsn)
    queue = PostgresTaskQueueStore(dsn)
    yield sched, queue
    try:
        conn = psycopg.connect(dsn)
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM schedules WHERE tenant_id IN (%s, %s)",
                (_TENANT, _OTHER),
            )
            cur.execute(
                "DELETE FROM task_queue WHERE tenant_id IN (%s, %s)",
                (_TENANT, _OTHER),
            )
        conn.commit()
        conn.close()
    except Exception:  # noqa: BLE001 — test cleanup must not fail the test
        pass


def _task(**overrides):
    payload = {"intent": "pg cron test", "title": "t", "workflow_type": "generic", "context": {}}
    payload.update(overrides)
    return payload


def test_pg_schedule_crud_and_tenant_scope(stores):
    sched, _queue = stores
    row = create_schedule(
        sched,
        tenant_id=_TENANT,
        owner_id="u1",
        name="morning",
        cron_expr="0 9 * * *",
        timezone="UTC",
        task=_task(),
    )
    assert row.next_fire_at is not None and row.enabled is True
    assert sched.get(_TENANT, row.id) is not None
    assert sched.get(_OTHER, row.id) is None
    assert len(sched.list_schedules(_TENANT)) == 1
    assert sched.list_schedules(_OTHER) == []
    updated = sched.update(_TENANT, row.id, name="evening")
    assert updated.name == "evening" and updated.cron_expr == "0 9 * * *"
    assert sched.update(_OTHER, row.id, name="nope") is None
    assert sched.delete(_OTHER, row.id) is False
    assert sched.delete(_TENANT, row.id) is True
    assert sched.get(_TENANT, row.id) is None


def test_pg_fire_due_end_to_end(stores):
    sched, queue = stores
    now = datetime(2026, 11, 17, 8, 0, tzinfo=UTC)
    row = create_schedule(
        sched,
        tenant_id=_TENANT,
        owner_id="u1",
        name="minutely",
        cron_expr="* * * * *",
        timezone="UTC",
        task=_task(),
        now=now - timedelta(minutes=3),
    )
    # Fires at T-2, T-1, T (the create instant T-3 is exclusive).
    report = fire_due_schedules(sched, queue, now=now)
    assert report.fired == 3 and report.schedules == 1
    rows = queue.list_tasks(_TENANT, limit=100)
    assert len(rows) == 3
    assert len({r.idempotency_key for r in rows}) == 3
    assert all(r.idempotency_key.startswith(f"schedule:{row.id}:") for r in rows)
    updated = sched.get(_TENANT, row.id)
    assert updated.next_fire_at == datetime(2026, 11, 17, 8, 1, tzinfo=UTC)
    assert updated.last_fire_at == now
    # A second fire at the same instant enqueues nothing new.
    report = fire_due_schedules(sched, queue, now=now)
    assert report.fired == 0
    assert len(queue.list_tasks(_TENANT, limit=100)) == 3


def test_pg_concurrent_fire_is_exactly_once(stores):
    sched, queue = stores
    now = datetime(2026, 11, 17, 8, 0, tzinfo=UTC)
    create_schedule(
        sched,
        tenant_id=_TENANT,
        owner_id="u1",
        name="minutely",
        cron_expr="* * * * *",
        timezone="UTC",
        task=_task(),
        now=now - timedelta(minutes=5),
    )
    # 5 fires due (T-4 … T). Two workers firing concurrently must
    # still enqueue exactly 5 rows — the deterministic key absorbs
    # the loser's replays, and only one advance wins.
    reports: list = []

    def _fire() -> None:
        reports.append(fire_due_schedules(sched, queue, now=now))

    threads = [threading.Thread(target=_fire) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r.fired for r in reports) == 5
    rows = queue.list_tasks(_TENANT, limit=100)
    assert len(rows) == 5
    assert len({r.idempotency_key for r in rows}) == 5
    # A third fire after both passes enqueues nothing new.
    assert fire_due_schedules(sched, queue, now=now).fired == 0


def test_pg_advance_expected_value(stores):
    sched, _queue = stores
    row = create_schedule(
        sched,
        tenant_id=_TENANT,
        owner_id="u1",
        name="x",
        cron_expr="0 9 * * *",
        timezone="UTC",
        task=_task(),
    )
    assert (
        sched.advance(
            _TENANT,
            row.id,
            expected_next_fire_at=datetime(2030, 1, 1, tzinfo=UTC),
            next_fire_at=datetime(2030, 1, 2, tzinfo=UTC),
            last_fire_at=None,
        )
        is None
    )
    advanced = sched.advance(
        _TENANT,
        row.id,
        expected_next_fire_at=row.next_fire_at,
        next_fire_at=next_fire("0 9 * * *", "UTC", row.next_fire_at),
        last_fire_at=row.next_fire_at,
    )
    assert advanced is not None
    assert advanced.next_fire_at == next_fire("0 9 * * *", "UTC", row.next_fire_at)
    assert advanced.last_fire_at == row.next_fire_at
