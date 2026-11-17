"""A5 slice 1 — durable task queue: store, service, worker, API.

Unit level: the InMemory store (the Postgres store is covered by
tests/integration/test_task_queue_pg.py). The worker runs against a
stubbed ``run_task`` so no model or graph is involved.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.queue.service import enqueue_task, normalise_task_payload
from forgeflow.queue.store import (
    InMemoryTaskQueueStore,
    backoff_delay,
    get_task_queue_store,
    reset_task_queue_store,
    set_task_queue_store,
)
from forgeflow.queue.worker import QueueWorker
from tests.business_auth import authenticate_business_app

_TENANT = "t-queue-unit"
_OTHER = "t-queue-other"


def _now() -> datetime:
    """Fresh 'now' per test — the store's lease checks use real time,
    so a frozen module-level timestamp would already be expired."""
    return datetime.now(UTC)


def _task(**overrides):
    payload = {"intent": "分析销售数据", "title": "t", "workflow_type": "generic", "context": {}}
    payload.update(overrides)
    return payload


@pytest.fixture(autouse=True)
def _clean_store():
    reset_task_queue_store()
    yield
    reset_task_queue_store()


@pytest.fixture()
def store():
    return InMemoryTaskQueueStore()


# --- service: payload + backoff ------------------------------------------- #


def test_normalise_task_payload_ok_and_defaults():
    out = normalise_task_payload({"intent": "  做点事  "})
    assert out == {"intent": "做点事", "title": "", "workflow_type": "generic", "context": {}}


def test_normalise_task_payload_rejects():
    for bad in ({}, {"intent": ""}, {"intent": "x", "context": []}, {"intent": "x", "title": 3}):
        with pytest.raises(ValueError):
            normalise_task_payload(bad)


def test_backoff_delay_doubles_and_caps():
    assert backoff_delay(1) == timedelta(seconds=30)
    assert backoff_delay(2) == timedelta(seconds=60)
    assert backoff_delay(3) == timedelta(seconds=120)
    assert backoff_delay(99) == timedelta(seconds=3600)


# --- store: enqueue idempotency + reads ------------------------------------ #


def test_enqueue_idempotent_same_key_returns_same_row(store):
    first = enqueue_task(store, tenant_id=_TENANT, owner_id="u1", task=_task(), idempotency_key="k1")
    second = enqueue_task(store, tenant_id=_TENANT, owner_id="u1", task=_task(), idempotency_key="k1")
    assert first.id == second.id
    assert len(store.list_tasks(_TENANT)) == 1


def test_enqueue_same_key_other_tenant_is_a_different_row(store):
    a = enqueue_task(store, tenant_id=_TENANT, owner_id="u1", task=_task(), idempotency_key="k1")
    b = enqueue_task(store, tenant_id=_OTHER, owner_id="u1", task=_task(), idempotency_key="k1")
    assert a.id != b.id


def test_get_is_tenant_scoped(store):
    row = enqueue_task(store, tenant_id=_TENANT, owner_id="u1", task=_task(), idempotency_key="k1")
    assert store.get(_TENANT, row.id) is not None
    assert store.get(_OTHER, row.id) is None
    assert store.get(_TENANT, "nope") is None


def test_list_newest_first_and_status_filter(store):
    a = enqueue_task(store, tenant_id=_TENANT, owner_id="u1", task=_task(), idempotency_key="k1")
    b = enqueue_task(store, tenant_id=_TENANT, owner_id="u1", task=_task(), idempotency_key="k2")
    ids = [row.id for row in store.list_tasks(_TENANT)]
    assert ids == [b.id, a.id]
    store.claim("w1", limit=1, lease_seconds=120, now=_now())
    assert [row.id for row in store.list_tasks(_TENANT, status="claimed")] != []
    assert store.list_tasks(_TENANT, status="done") == []


# --- store: claim / run / complete / fail --------------------------------- #


def test_claim_exclusive_and_oldest_due_first(store):
    first = enqueue_task(store, tenant_id=_TENANT, owner_id="u1", task=_task(), idempotency_key="k1")
    second = enqueue_task(store, tenant_id=_TENANT, owner_id="u1", task=_task(), idempotency_key="k2")
    # No explicit ``now``: the claim uses real time, so both rows
    # (enqueued just before) are due.
    claimed = store.claim("w1", limit=1, lease_seconds=120)
    assert [row.id for row in claimed] == [first.id]
    assert claimed[0].status == "claimed" and claimed[0].claimed_by == "w1"
    # The other worker gets the OTHER row — never w1's row: claims are disjoint.
    claimed = store.claim("w2", limit=5, lease_seconds=120)
    assert [row.id for row in claimed] == [second.id]
    assert store.claim("w3", limit=5, lease_seconds=120) == []


def test_claim_skips_not_yet_due_rows(store):
    row = enqueue_task(store, tenant_id=_TENANT, owner_id="u1", task=_task(), idempotency_key="k1")
    store.claim("w1", limit=1, lease_seconds=120)
    store.mark_running(_TENANT, row.id, "w1")
    now = _now()
    failed = store.fail(_TENANT, row.id, "w1", "boom", now=now)
    assert failed is not None and failed.status == "failed"
    assert failed.next_attempt_at == now + timedelta(seconds=30)
    assert store.claim("w2", limit=5, lease_seconds=120, now=now) == []
    retry = store.claim("w2", limit=5, lease_seconds=120, now=now + timedelta(seconds=31))
    assert [row.id for row in retry] == [failed.id]


def test_fail_to_max_attempts_goes_dead_and_is_never_claimed(store):
    row = enqueue_task(
        store, tenant_id=_TENANT, owner_id="u1", task=_task(), idempotency_key="k1", max_attempts=2
    )
    for attempt in range(2):
        store.claim("w1", limit=1, lease_seconds=120)
        store.mark_running(_TENANT, row.id, "w1")
        out = store.fail(_TENANT, row.id, "w1", "boom", now=_now())
        if attempt == 0:
            # The retry is backoff-gated (30 s); force it due so the
            # second attempt can run in this test.
            row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    assert out is not None and out.status == "dead" and out.attempts == 2
    assert store.claim("w1", limit=5, lease_seconds=120, now=_now() + timedelta(days=1)) == []


def test_complete_requires_the_lease_holder(store):
    row = enqueue_task(store, tenant_id=_TENANT, owner_id="u1", task=_task(), idempotency_key="k1")
    store.claim("w1", limit=1, lease_seconds=120)
    assert store.complete(_TENANT, row.id, "w2", "run-1") is None
    assert store.mark_running(_TENANT, row.id, "w2") is None
    done = store.mark_running(_TENANT, row.id, "w1")
    assert done is not None and done.status == "running"
    done = store.complete(_TENANT, row.id, "w1", "run-1")
    assert done is not None and done.status == "done" and done.run_id == "run-1"


def test_reap_expired_lease_returns_row_to_queued(store):
    row = enqueue_task(store, tenant_id=_TENANT, owner_id="u1", task=_task(), idempotency_key="k1")
    # ``now`` is captured AFTER the enqueue so the row is due at claim time.
    now = _now()
    store.claim("w1", limit=1, lease_seconds=60, now=now)
    assert store.reap_expired_leases(now=now + timedelta(seconds=59)) == 0
    assert store.reap_expired_leases(now=now + timedelta(seconds=61)) == 1
    assert store.get(_TENANT, row.id).status == "queued"
    # The old holder can no longer complete it.
    assert store.complete(_TENANT, row.id, "w1", "run-1") is None
    # A new worker can claim and finish it.
    assert [r.id for r in store.claim("w2", limit=1, lease_seconds=120, now=now)] == [row.id]


# --- worker ---------------------------------------------------------------- #


class _FakeHandle:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id


async def _ok_run(task, ctx):
    return _FakeHandle("run-ok")


async def _boom_run(task, ctx):
    raise RuntimeError("model exploded")


async def test_worker_runs_claimed_task_to_done(store):
    enqueue_task(store, tenant_id=_TENANT, owner_id="u1", task=_task(), idempotency_key="k1")
    worker = QueueWorker(store=store, run_task_fn=_ok_run)
    report = await worker.run_once()
    assert report.to_dict() == {
        "reaped": 0,
        "claimed": 1,
        "done": 1,
        "failed": 0,
        "dead": 0,
        "skipped": 0,
    }
    rows = store.list_tasks(_TENANT)
    assert rows[0].status == "done" and rows[0].run_id == "run-ok"


async def test_worker_failure_retries_then_dies(store):
    enqueue_task(
        store, tenant_id=_TENANT, owner_id="u1", task=_task(), idempotency_key="k1", max_attempts=2
    )
    worker = QueueWorker(store=store, run_task_fn=_boom_run)
    report = await worker.run_once()
    assert report.failed == 1 and report.dead == 0
    row = store.list_tasks(_TENANT)[0]
    assert row.status == "failed" and row.last_error == "model exploded"
    # Second pass while the backoff has not elapsed claims nothing.
    report = await worker.run_once()
    assert report.claimed == 0
    # Force the retry due by moving the row's next attempt into the past.
    row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    report = await worker.run_once()
    assert report.dead == 1
    assert store.list_tasks(_TENANT)[0].status == "dead"


async def test_worker_sets_tenant_scope_for_the_run(store):
    """The run executes under the row's tenant scope: durable
    writers inside a run that read only the ambient tenant (the
    event log) must see the queued row's tenant, and the scope must
    be reset once the run returns."""
    from forgeflow.tenant_scope import current_tenant

    seen: dict[str, str | None] = {}

    async def _capture(task, ctx):
        seen["tenant"] = current_tenant()
        return _FakeHandle("run-1")

    enqueue_task(store, tenant_id=_TENANT, owner_id="owner-7", task=_task(), idempotency_key="k1")
    worker = QueueWorker(store=store, run_task_fn=_capture)
    await worker.run_once()
    assert seen == {"tenant": _TENANT}
    assert current_tenant() is None


async def test_worker_uses_service_role_and_owner_identity(store):
    seen: dict[str, str] = {}

    async def _capture(task, ctx):
        seen["role"] = ctx.role
        seen["user_id"] = ctx.user_id
        seen["tenant_id"] = ctx.tenant_id
        seen["intent"] = task.intent
        return _FakeHandle("run-1")

    enqueue_task(store, tenant_id=_TENANT, owner_id="owner-7", task=_task(), idempotency_key="k1")
    worker = QueueWorker(store=store, run_task_fn=_capture)
    await worker.run_once()
    assert seen == {
        "role": "service",
        "user_id": "owner-7",
        "tenant_id": _TENANT,
        "intent": "分析销售数据",
    }


# --- API ------------------------------------------------------------------- #


def _api_app(store, tenant_override: str | None = None):
    from forgeflow.api.routers import queue as queue_router

    set_task_queue_store(store)
    app = FastAPI()
    authenticate_business_app(app)
    if tenant_override is not None:
        app.dependency_overrides[resolve_tenant] = lambda: tenant_override
    app.include_router(queue_router.router, prefix="/queue")
    return app


def test_api_enqueue_list_get_and_replay(store):
    client = TestClient(_api_app(store))
    resp = client.post(
        "/queue/tasks",
        json={"intent": "分析销售数据", "idempotency_key": "api-k1", "max_attempts": 2},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "queued" and body["max_attempts"] == 2
    assert body["task"]["intent"] == "分析销售数据"
    task_id = body["id"]

    replay = client.post(
        "/queue/tasks",
        json={"intent": "分析销售数据", "idempotency_key": "api-k1"},
    )
    assert replay.status_code == 200 and replay.json()["id"] == task_id

    listed = client.get("/queue/tasks")
    assert listed.status_code == 200 and [row["id"] for row in listed.json()] == [task_id]

    got = client.get(f"/queue/tasks/{task_id}")
    assert got.status_code == 200 and got.json()["id"] == task_id
    assert client.get("/queue/tasks/no-such-id").status_code == 404


def test_api_enqueue_rejects_empty_intent(store):
    client = TestClient(_api_app(store))
    assert client.post("/queue/tasks", json={"intent": "  "}).status_code == 422


def test_api_get_is_tenant_scoped(store):
    client = TestClient(_api_app(store))
    created = client.post("/queue/tasks", json={"intent": "x", "idempotency_key": "k"})
    task_id = created.json()["id"]
    other = TestClient(_api_app(store, tenant_override=_OTHER))
    assert other.get(f"/queue/tasks/{task_id}").status_code == 404
    assert other.get("/queue/tasks").json() == []


def test_get_task_queue_store_selector(monkeypatch):
    # conftest sets a sync DSN default, so the selector must be
    # tested with the env controlled, not assumed.
    monkeypatch.delenv("POSTGRES_SYNC_URL", raising=False)
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    reset_task_queue_store()
    assert get_task_queue_store().durability == "ephemeral"
    monkeypatch.setenv("POSTGRES_SYNC_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    reset_task_queue_store()
    assert get_task_queue_store().durability == "durable"
