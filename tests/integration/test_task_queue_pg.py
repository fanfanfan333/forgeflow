"""A5 slice 1: Postgres task queue store against the real ``task_queue`` table.

Skips when live Postgres is unreachable or migration 046 has not
been applied (the migration's own application is verified by the
alembic upgrade in the verification run, not by this test).
"""

from __future__ import annotations

import os
import threading
import time
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from forgeflow.queue.service import enqueue_task
from forgeflow.queue.store import PostgresTaskQueueStore

_TENANT = "t-queue-pg"
_OTHER = "t-queue-pg-other"


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
            cur.execute("SELECT 1 FROM task_queue LIMIT 1")
    except Exception:  # noqa: BLE001
        conn.close()
        pytest.skip("task_queue table missing (migration 046 not applied)")
    conn.close()
    pg = PostgresTaskQueueStore(dsn)
    yield pg
    try:
        conn = psycopg.connect(dsn)
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM task_queue WHERE tenant_id IN (%s, %s)",
                (_TENANT, _OTHER),
            )
        conn.commit()
        conn.close()
    except Exception:  # noqa: BLE001 — test cleanup must not fail the test
        pass


def _task(**overrides):
    payload = {"intent": "pg queue test", "title": "t", "workflow_type": "generic", "context": {}}
    payload.update(overrides)
    return payload


def _now():
    return datetime.now(UTC)


def test_pg_enqueue_idempotent_and_tenant_scoped(store):
    first = enqueue_task(store, tenant_id=_TENANT, owner_id="u1", task=_task(), idempotency_key="k1")
    second = enqueue_task(store, tenant_id=_TENANT, owner_id="u1", task=_task(), idempotency_key="k1")
    assert first.id == second.id
    assert len(store.list_tasks(_TENANT)) == 1
    other = enqueue_task(store, tenant_id=_OTHER, owner_id="u1", task=_task(), idempotency_key="k1")
    assert other.id != first.id
    assert store.get(_OTHER, first.id) is None
    assert store.get(_TENANT, first.id).status == "queued"


def test_pg_claim_fail_retry_dead(store):
    row = enqueue_task(
        store, tenant_id=_TENANT, owner_id="u1", task=_task(), idempotency_key="k1", max_attempts=2
    )
    claimed = store.claim("w1", limit=1, lease_seconds=120)
    assert [r.id for r in claimed] == [row.id]
    assert store.claim("w2", limit=5, lease_seconds=120) == []
    assert store.mark_running(_TENANT, row.id, "w1").status == "running"
    failed = store.fail(_TENANT, row.id, "w1", "boom", now=_now())
    assert failed.status == "failed" and failed.attempts == 1
    # Backoff not elapsed: nothing claimable yet.
    assert store.claim("w2", limit=5, lease_seconds=120) == []
    # Second attempt → dead.
    with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE task_queue SET next_attempt_at = now() - interval '1 second' "
            "WHERE id = %s",
            (row.id,),
        )
        conn.commit()
    claimed = store.claim("w2", limit=1, lease_seconds=120)
    assert [r.id for r in claimed] == [row.id]
    store.mark_running(_TENANT, row.id, "w2")
    dead = store.fail(_TENANT, row.id, "w2", "boom again", now=_now())
    assert dead.status == "dead" and dead.attempts == 2
    assert store.claim("w1", limit=5, lease_seconds=120) == []


def test_pg_concurrent_claims_are_disjoint(store):
    ids = [
        enqueue_task(store, tenant_id=_TENANT, owner_id="u1", task=_task(), idempotency_key=f"k{i}").id
        for i in range(6)
    ]
    results: dict[str, list[str]] = {}

    def _claim(worker: str) -> None:
        results[worker] = [r.id for r in store.claim(worker, limit=10, lease_seconds=120)]

    threads = [threading.Thread(target=_claim, args=(f"w{i}",)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    claimed = [row_id for rows in results.values() for row_id in rows]
    assert sorted(claimed) == sorted(ids)
    assert len(set(claimed)) == len(claimed)


def test_pg_reap_and_complete(store):
    row = enqueue_task(store, tenant_id=_TENANT, owner_id="u1", task=_task(), idempotency_key="k1")
    store.claim("w1", limit=1, lease_seconds=1)
    assert store.reap_expired_leases() == 0
    time.sleep(1.2)
    assert store.reap_expired_leases() == 1
    assert store.get(_TENANT, row.id).status == "queued"
    assert store.complete(_TENANT, row.id, "w1", "run-1") is None
    store.claim("w2", limit=1, lease_seconds=120)
    store.mark_running(_TENANT, row.id, "w2")
    done = store.complete(_TENANT, row.id, "w2", "run-9")
    assert done.status == "done" and done.run_id == "run-9"
    assert store.list_tasks(_TENANT, status="done")[0].id == row.id
