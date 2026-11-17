"""A5 slice 4 — worker heartbeats: store, worker hook, API.

Unit level: the InMemory store (the Postgres store is covered by
tests/integration/test_queue_worker_heartbeat_pg.py). The worker
runs against a stubbed ``run_task`` so no model or graph is involved.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.middleware.auth import RBACMiddleware
from forgeflow.queue.heartbeats import (
    DEFAULT_POLL_SECONDS,
    STALE_AFTER_MULTIPLIER,
    InMemoryHeartbeatStore,
    get_heartbeat_store,
    reset_heartbeat_store,
)
from forgeflow.queue.service import enqueue_task
from forgeflow.queue.store import InMemoryTaskQueueStore
from forgeflow.queue.worker import QueueWorker
from tests.business_auth import authenticate_business_app

_TENANT = "t-heartbeat-unit"


def _now() -> datetime:
    return datetime.now(UTC)


def _task():
    return {"intent": "分析销售数据", "title": "t", "workflow_type": "generic", "context": {}}


@pytest.fixture(autouse=True)
def _clean_stores():
    reset_heartbeat_store()
    yield
    reset_heartbeat_store()


@pytest.fixture()
def beats():
    return InMemoryHeartbeatStore()


# --- store ----------------------------------------------------------------- #


def test_beat_upsert_keeps_started_at(beats):
    first = beats.beat(
        "w-1",
        hostname="h1",
        started_at=_now() - timedelta(minutes=10),
        last_report={"claimed": 1},
    )
    assert first.started_at is not None
    assert first.last_report == {"claimed": 1}
    second = beats.beat(
        "w-1",
        hostname="h1",
        started_at=_now(),
        last_report={"claimed": 2, "done": 1},
    )
    # A later beat never rewrites the process start.
    assert second.started_at == first.started_at
    assert second.last_report == {"claimed": 2, "done": 1}
    assert second.last_beat_at >= first.last_beat_at
    assert beats.get("w-1").worker_id == "w-1"
    assert beats.get("no-such-worker") is None


def test_list_heartbeats_newest_first(beats):
    base = _now()
    beats.beat("w-old", hostname="h", started_at=base, last_report={}, now=base)
    beats.beat(
        "w-new", hostname="h", started_at=base, last_report={}, now=base + timedelta(seconds=5)
    )
    rows = beats.list_heartbeats()
    assert [r.worker_id for r in rows] == ["w-new", "w-old"]
    assert beats.list_heartbeats(limit=1)[0].worker_id == "w-new"


def test_is_stale_uses_three_poll_intervals(beats):
    base = _now()
    row = beats.beat(
        "w-1",
        hostname="h",
        started_at=base,
        last_report={"poll_seconds": 5},
        now=base,
    )
    assert row.poll_seconds() == 5
    assert row.is_stale(base + timedelta(seconds=14)) is False
    assert row.is_stale(base + timedelta(seconds=16)) is True
    # A row with no poll interval falls back to the default.
    defaulted = beats.beat("w-2", hostname="h", started_at=base, last_report={}, now=base)
    assert defaulted.poll_seconds() == DEFAULT_POLL_SECONDS
    assert defaulted.is_stale(base + timedelta(seconds=STALE_AFTER_MULTIPLIER * 5 + 1)) is True
    # No beat at all is stale.
    assert beats.get("w-3") is None


# --- worker hook ----------------------------------------------------------- #


class _FakeHandle:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id


async def _ok_run(task, ctx):
    return _FakeHandle("run-ok")


async def test_worker_beats_once_per_pass(beats):
    queue = InMemoryTaskQueueStore()
    enqueue_task(queue, tenant_id=_TENANT, owner_id="u1", task=_task(), idempotency_key="k1")
    worker = QueueWorker(store=queue, heartbeat_store=beats, run_task_fn=_ok_run)
    report = await worker.run_once()
    assert report.done == 1
    row = beats.get(worker.worker_id)
    assert row is not None
    assert row.hostname == worker.hostname
    assert row.started_at is not None and row.last_beat_at is not None
    assert row.last_report["claimed"] == 1
    assert row.last_report["done"] == 1
    assert row.last_report["poll_seconds"] == worker.poll_seconds
    assert row.is_stale() is False


async def test_worker_beat_failure_never_fails_the_pass():
    """A dead heartbeat store must not break claim/run: the beat is
    observability, not the work."""

    class _DeadBeats:
        durability = "ephemeral"

        def beat(self, *args, **kwargs):
            raise RuntimeError("heartbeat store down")

        def get(self, worker_id):
            return None

        def list_heartbeats(self, *, limit=20):
            return []

    queue = InMemoryTaskQueueStore()
    enqueue_task(queue, tenant_id=_TENANT, owner_id="u1", task=_task(), idempotency_key="k1")
    worker = QueueWorker(store=queue, heartbeat_store=_DeadBeats(), run_task_fn=_ok_run)
    report = await worker.run_once()
    assert report.done == 1
    assert queue.list_tasks(_TENANT)[0].status == "done"


async def test_worker_startup_beat_is_visible_before_first_pass():
    queue = InMemoryTaskQueueStore()
    beats_store = InMemoryHeartbeatStore()
    worker = QueueWorker(
        store=queue, heartbeat_store=beats_store, run_task_fn=_ok_run, poll_seconds=60
    )
    worker.start()
    try:
        # The startup beat is the first thing the loop does; poll
        # briefly rather than assuming a fixed instant.
        for _ in range(50):
            if beats_store.get(worker.worker_id) is not None:
                break
            await asyncio.sleep(0.05)
        row = beats_store.get(worker.worker_id)
        assert row is not None
        assert row.last_report["poll_seconds"] == 60
    finally:
        await worker.stop()


# --- API ------------------------------------------------------------------- #


def _api_app(beats_store):
    from forgeflow.api.routers import queue as queue_router

    from forgeflow.queue.heartbeats import set_heartbeat_store

    set_heartbeat_store(beats_store)
    app = FastAPI()
    authenticate_business_app(app)
    app.include_router(queue_router.router, prefix="/queue")
    return app


def test_api_list_workers_newest_first_and_stale_flag(beats):
    base = _now()
    beats.beat(
        "w-stale",
        hostname="h",
        started_at=base,
        last_report={"poll_seconds": 5},
        now=base - timedelta(seconds=60),
    )
    beats.beat("w-fresh", hostname="h", started_at=base, last_report={"poll_seconds": 5}, now=base)
    client = TestClient(_api_app(beats))
    resp = client.get("/queue/workers")
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert [r["worker_id"] for r in rows] == ["w-fresh", "w-stale"]
    assert rows[0]["stale"] is False
    assert rows[1]["stale"] is True
    assert rows[0]["last_report"] == {"poll_seconds": 5}


def test_workers_route_is_rbac_mapped():
    assert RBACMiddleware._resolve_permission("GET", "/queue/workers") == (
        "read",
        "workflows",
    )


def test_get_heartbeat_store_selector(monkeypatch):
    # conftest sets a sync DSN default, so the selector must be
    # tested with the env controlled, not assumed.
    monkeypatch.delenv("POSTGRES_SYNC_URL", raising=False)
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    reset_heartbeat_store()
    assert get_heartbeat_store().durability == "ephemeral"
    monkeypatch.setenv("POSTGRES_SYNC_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    reset_heartbeat_store()
    assert get_heartbeat_store().durability == "durable"
