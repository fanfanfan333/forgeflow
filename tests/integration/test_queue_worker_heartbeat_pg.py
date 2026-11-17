"""A5 slice 4: Postgres worker heartbeat store against the real
``worker_heartbeats`` table.

Skips when live Postgres is unreachable or migration 049 has not
been applied (the migration's own application is verified by the
alembic upgrade in the verification run, not by this test).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from forgeflow.queue.heartbeats import PostgresHeartbeatStore
from forgeflow.queue.store import InMemoryTaskQueueStore
from forgeflow.queue.worker import QueueWorker

_WORKER = "w-hb-pg-1"
_OTHER = "w-hb-pg-2"


def _dsn():
    dsn = os.environ.get("POSTGRES_SYNC_URL") or os.environ.get("POSTGRES_DSN") or ""
    return dsn.replace("postgresql+psycopg://", "postgresql://")


@pytest.fixture
def store():
    dsn = _dsn()
    if not dsn:
        pytest.skip("no sync Postgres DSN")
    try:
        conn = psycopg.connect(dsn)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"live Postgres not reachable ({exc})")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM worker_heartbeats LIMIT 1")
    except Exception:  # noqa: BLE001
        conn.close()
        pytest.skip("worker_heartbeats table missing (migration 049 not applied)")
    conn.close()
    pg = PostgresHeartbeatStore(dsn)
    yield pg
    try:
        conn = psycopg.connect(dsn)
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM worker_heartbeats WHERE worker_id IN (%s, %s)",
                (_WORKER, _OTHER),
            )
        conn.commit()
        conn.close()
    except Exception:  # noqa: BLE001 — test cleanup must not fail the test
        pass


def _now():
    return datetime.now(UTC)


def test_beat_upsert_keeps_started_at(store):
    base = _now()
    first = store.beat(
        _WORKER,
        hostname="h1",
        started_at=base - timedelta(minutes=10),
        last_report={"claimed": 1},
    )
    assert first.started_at is not None
    second = store.beat(
        _WORKER,
        hostname="h1",
        started_at=base,
        last_report={"claimed": 2, "done": 1},
    )
    # A later beat never rewrites the process start.
    assert second.started_at == first.started_at
    assert second.last_report == {"claimed": 2, "done": 1}
    assert store.get(_WORKER).worker_id == _WORKER
    assert store.get("no-such-worker") is None


def test_list_heartbeats_newest_first_and_stale(store):
    base = _now()
    store.beat(
        _OTHER,
        hostname="h",
        started_at=base,
        last_report={"poll_seconds": 5},
        now=base - timedelta(seconds=60),
    )
    store.beat(_WORKER, hostname="h", started_at=base, last_report={"poll_seconds": 5}, now=base)
    rows = store.list_heartbeats()
    assert [r.worker_id for r in rows] == [_WORKER, _OTHER]
    assert rows[0].is_stale() is False
    assert rows[1].is_stale() is True


class _FakeHandle:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id


async def test_worker_run_once_writes_a_pg_heartbeat(store):
    """The worker's pass beat lands in Postgres with the pass counts."""
    queue = InMemoryTaskQueueStore()

    async def _ok_run(task, ctx):
        return _FakeHandle("run-ok")

    worker = QueueWorker(store=queue, heartbeat_store=store, worker_id=_WORKER, run_task_fn=_ok_run)
    report = await worker.run_once()
    assert report.claimed == 0
    row = store.get(_WORKER)
    assert row is not None
    assert row.hostname == worker.hostname
    assert row.last_report["poll_seconds"] == worker.poll_seconds
    assert row.last_report["claimed"] == 0
    assert row.is_stale() is False
