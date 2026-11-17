"""A5 slice 2 — cron schedules: validation, next-fire, firing,
worker scheduler pass, API.

Unit level: the InMemory stores (the Postgres store is covered by
tests/integration/test_cron_schedules_pg.py). The worker runs
against a stubbed ``run_task`` so no model or graph is involved.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.middleware.auth import RBACMiddleware
from forgeflow.queue.schedules import (
    MAX_CATCHUP_FIRES,
    InMemoryScheduleStore,
    create_schedule,
    fire_due_schedules,
    get_schedule_store,
    next_fire,
    reset_schedule_store,
    set_schedule_store,
    update_schedule,
    validate_cron_expr,
    validate_timezone,
)
from forgeflow.queue.store import (
    InMemoryTaskQueueStore,
    reset_task_queue_store,
    set_task_queue_store,
)
from forgeflow.queue.worker import QueueWorker
from tests.business_auth import authenticate_business_app

_TENANT = "t-cron-unit"
_OTHER = "t-cron-other"


def _now() -> datetime:
    return datetime.now(UTC)


def _task(**overrides):
    payload = {"intent": "每日汇总", "title": "t", "workflow_type": "generic", "context": {}}
    payload.update(overrides)
    return payload


@pytest.fixture(autouse=True)
def _clean_stores():
    reset_task_queue_store()
    reset_schedule_store()
    yield
    reset_task_queue_store()
    reset_schedule_store()


@pytest.fixture()
def schedules():
    return InMemoryScheduleStore()


@pytest.fixture()
def queue():
    return InMemoryTaskQueueStore()


# --- validation ------------------------------------------------------------ #


def test_validate_cron_expr_ok():
    assert validate_cron_expr(" 0 9 * * * ") == "0 9 * * *"
    assert validate_cron_expr("*/15 8-18 * * 1-5") == "*/15 8-18 * * 1-5"


def test_validate_cron_expr_rejects():
    for bad in (
        "",
        "   ",
        "0 9 * *",  # 4 fields
        "0 0 9 * * *",  # 6 fields (seconds) — not this platform's contract
        "@daily",  # shortcut — not this platform's contract
        "99 * * * *",  # bad minute
        "0 9 * * 8",  # bad weekday
        "31 2 31 2 *",  # can never fire (Feb 31)
        "0 0 30 2 *",  # can never fire (Feb 30)
        123,  # type: ignore[arg-type] — not a string
    ):
        with pytest.raises(ValueError):
            validate_cron_expr(bad)


def test_validate_timezone_ok_and_rejects():
    assert validate_timezone("UTC") == "UTC"
    assert validate_timezone(" Asia/Shanghai ") == "Asia/Shanghai"
    for bad in ("", "Mars/Olympus", "Not/AZone", 5):  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            validate_timezone(bad)


# --- next_fire -------------------------------------------------------------- #


def test_next_fire_utc():
    after = datetime(2026, 11, 17, 8, 0, tzinfo=UTC)
    assert next_fire("0 9 * * *", "UTC", after) == datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
    # Strictly after: exactly on a fire time moves to the next one.
    assert next_fire("0 9 * * *", "UTC", datetime(2026, 11, 17, 9, 0, tzinfo=UTC)) == datetime(
        2026, 11, 18, 9, 0, tzinfo=UTC
    )


def test_next_fire_timezone_wall_clock():
    # 09:00 in Shanghai is 01:00 UTC. After 08:00 UTC the next
    # Shanghai 09:00 is tomorrow 01:00 UTC, not today.
    after = datetime(2026, 11, 17, 8, 0, tzinfo=UTC)
    assert next_fire("0 9 * * *", "Asia/Shanghai", after) == datetime(
        2026, 11, 18, 1, 0, tzinfo=UTC
    )


def test_next_fire_dst_transition():
    # America/New_York springs forward 2026-03-08 02:00 → 03:00.
    # A 09:00-local daily fire still lands on 09:00 local (14:00
    # UTC after the switch, 15:00 UTC before it — EST is UTC-5).
    before = datetime(2026, 3, 7, 12, 0, tzinfo=UTC)  # 07:00 EST
    assert next_fire("0 9 * * *", "America/New_York", before) == datetime(
        2026, 3, 7, 14, 0, tzinfo=UTC
    )
    after = datetime(2026, 3, 8, 12, 0, tzinfo=UTC)  # 08:00 EDT
    assert next_fire("0 9 * * *", "America/New_York", after) == datetime(
        2026, 3, 8, 13, 0, tzinfo=UTC
    )


def test_next_fire_naive_after_is_utc():
    naive = datetime(2026, 11, 17, 8, 0)
    assert next_fire("0 9 * * *", "UTC", naive) == datetime(2026, 11, 17, 9, 0, tzinfo=UTC)


# --- create / update / delete ------------------------------------------------ #


def test_create_computes_first_fire(schedules):
    now = datetime(2026, 11, 17, 8, 0, tzinfo=UTC)
    row = create_schedule(
        schedules,
        tenant_id=_TENANT,
        owner_id="u1",
        name="morning",
        cron_expr="0 9 * * *",
        timezone="UTC",
        task=_task(),
        now=now,
    )
    assert row.next_fire_at == datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
    assert row.last_fire_at is None and row.enabled is True
    assert schedules.get(_TENANT, row.id) is not None
    assert schedules.get(_OTHER, row.id) is None


def test_create_rejects_bad_inputs(schedules):
    for kwargs in (
        {"name": "  ", "cron_expr": "0 9 * * *", "task": _task()},
        {"name": "x", "cron_expr": "not a cron", "task": _task()},
        {"name": "x", "cron_expr": "0 9 * * *", "timezone": "Nope/Zone", "task": _task()},
        {"name": "x", "cron_expr": "0 9 * * *", "task": {"intent": ""}},
    ):
        with pytest.raises(ValueError):
            create_schedule(
                schedules,
                tenant_id=_TENANT,
                owner_id="u1",
                **kwargs,  # type: ignore[arg-type]
            )


def test_update_recomputes_next_fire_only_on_cron_or_timezone(schedules):
    now = datetime(2026, 11, 17, 8, 0, tzinfo=UTC)
    row = create_schedule(
        schedules,
        tenant_id=_TENANT,
        owner_id="u1",
        name="morning",
        cron_expr="0 9 * * *",
        timezone="UTC",
        task=_task(),
        now=now,
    )
    first = row.next_fire_at
    # Task-only patch leaves the next fire untouched.
    patched = update_schedule(
        schedules, tenant_id=_TENANT, schedule_id=row.id, task=_task(title="new"), now=now
    )
    assert patched is not None and patched.next_fire_at == first
    assert patched.task["title"] == "new"
    # Cron change recomputes from now.
    patched = update_schedule(
        schedules, tenant_id=_TENANT, schedule_id=row.id, cron_expr="0 10 * * *", now=now
    )
    assert patched is not None
    assert patched.next_fire_at == datetime(2026, 11, 17, 10, 0, tzinfo=UTC)
    # Timezone change recomputes too (10:00 Shanghai = 02:00 UTC).
    patched = update_schedule(
        schedules,
        tenant_id=_TENANT,
        schedule_id=row.id,
        timezone="Asia/Shanghai",
        now=now,
    )
    assert patched is not None
    assert patched.next_fire_at == datetime(2026, 11, 18, 2, 0, tzinfo=UTC)
    # Bad values are rejected and the row is untouched.
    with pytest.raises(ValueError):
        update_schedule(
            schedules, tenant_id=_TENANT, schedule_id=row.id, cron_expr="bogus", now=now
        )
    assert schedules.get(_TENANT, row.id).cron_expr == "0 10 * * *"
    assert update_schedule(schedules, tenant_id=_TENANT, schedule_id="nope") is None
    assert update_schedule(schedules, tenant_id=_OTHER, schedule_id=row.id) is None


def test_delete_is_tenant_scoped(schedules):
    row = create_schedule(
        schedules,
        tenant_id=_TENANT,
        owner_id="u1",
        name="x",
        cron_expr="0 9 * * *",
        task=_task(),
    )
    assert schedules.delete(_OTHER, row.id) is False
    assert schedules.delete(_TENANT, row.id) is True
    assert schedules.get(_TENANT, row.id) is None
    assert schedules.delete(_TENANT, row.id) is False


def test_list_enabled_filter(schedules):
    a = create_schedule(
        schedules, tenant_id=_TENANT, owner_id="u1", name="a", cron_expr="0 9 * * *", task=_task()
    )
    create_schedule(
        schedules,
        tenant_id=_TENANT,
        owner_id="u1",
        name="b",
        cron_expr="0 9 * * *",
        task=_task(),
        enabled=False,
    )
    assert [r.id for r in schedules.list_schedules(_TENANT, enabled=True)] == [a.id]
    assert len(schedules.list_schedules(_TENANT)) == 2
    assert schedules.list_schedules(_OTHER) == []


# --- firing ------------------------------------------------------------------ #


def test_fire_due_enqueues_once_and_advances(schedules, queue):
    now = datetime(2026, 11, 17, 8, 0, tzinfo=UTC)
    row = create_schedule(
        schedules,
        tenant_id=_TENANT,
        owner_id="u1",
        name="minutely",
        cron_expr="* * * * *",
        timezone="UTC",
        task=_task(),
        now=now - timedelta(minutes=3),
    )
    # First fire is 3 minutes ago; firing at `now` catches up 3 fires
    # (T-2, T-1, T — croniter's next is exclusive of the create instant,
    # so T-3 itself is not a fire) — one queue row each, distinct keys.
    report = fire_due_schedules(schedules, queue, now=now)
    assert report.fired == 3 and report.schedules == 1
    assert len(queue.list_tasks(_TENANT)) == 3
    assert len({r.idempotency_key for r in queue.list_tasks(_TENANT)}) == 3
    assert all(k.startswith(f"schedule:{row.id}:") for k in
               [r.idempotency_key for r in queue.list_tasks(_TENANT)])
    updated = schedules.get(_TENANT, row.id)
    assert updated.next_fire_at == datetime(2026, 11, 17, 8, 1, tzinfo=UTC)
    assert updated.last_fire_at == now
    # A second fire at the same instant is a no-op (nothing due).
    report = fire_due_schedules(schedules, queue, now=now)
    assert report.fired == 0 and report.schedules == 0
    assert len(queue.list_tasks(_TENANT)) == 3


def test_fire_due_skips_disabled(schedules, queue):
    now = _now()
    create_schedule(
        schedules,
        tenant_id=_TENANT,
        owner_id="u1",
        name="off",
        cron_expr="* * * * *",
        timezone="UTC",
        task=_task(),
        enabled=False,
        now=now - timedelta(minutes=5),
    )
    assert fire_due_schedules(schedules, queue, now=now).fired == 0
    assert queue.list_tasks(_TENANT) == []


def test_fire_due_catch_up_cap(schedules, queue):
    now = datetime(2026, 11, 17, 8, 0, tzinfo=UTC)
    row = create_schedule(
        schedules,
        tenant_id=_TENANT,
        owner_id="u1",
        name="minutely",
        cron_expr="* * * * *",
        timezone="UTC",
        task=_task(),
        now=now - timedelta(minutes=201),
    )
    # 201 fires due (04:40 … 08:00 — the create instant 04:39 is
    # exclusive, so the first fire is 04:40); one pass fires at most
    # the cap.
    report = fire_due_schedules(schedules, queue, now=now)
    assert report.fired == MAX_CATCHUP_FIRES and report.schedules == 1
    updated = schedules.get(_TENANT, row.id)
    assert updated.next_fire_at <= now  # not fully caught up yet
    report = fire_due_schedules(schedules, queue, now=now)
    assert report.fired == MAX_CATCHUP_FIRES
    report = fire_due_schedules(schedules, queue, now=now)
    assert report.fired == 1  # the last fire
    assert len(queue.list_tasks(_TENANT, limit=300)) == 201
    assert schedules.get(_TENANT, row.id).next_fire_at > now


def test_fire_due_advance_race_counts_nothing(schedules, queue):
    """A lost advance race (another worker already advanced the row)
    fires nothing twice: the row's next fire is past `now`, and a
    later pass fires the next minute exactly once."""
    now = datetime(2026, 11, 17, 8, 0, tzinfo=UTC)
    row = create_schedule(
        schedules,
        tenant_id=_TENANT,
        owner_id="u1",
        name="minutely",
        cron_expr="* * * * *",
        timezone="UTC",
        task=_task(),
        now=now - timedelta(minutes=1),
    )
    # Simulate the other worker winning: advance the row first.
    first = schedules.get(_TENANT, row.id)
    schedules.advance(
        _TENANT,
        row.id,
        expected_next_fire_at=first.next_fire_at,
        next_fire_at=next_fire("* * * * *", "UTC", first.next_fire_at),
        last_fire_at=first.next_fire_at,
    )
    # The firing pass sees the advanced next fire (08:01, not due
    # at 08:00) and counts nothing — the 08:00 fire is not re-fired.
    report = fire_due_schedules(schedules, queue, now=now)
    assert report.fired == 0
    assert queue.list_tasks(_TENANT) == []
    # A later pass fires the next minute exactly once.
    later = fire_due_schedules(schedules, queue, now=now + timedelta(minutes=1))
    assert later.fired == 1
    assert len(queue.list_tasks(_TENANT)) == 1


def test_advance_wrong_expected_returns_none(schedules):
    row = create_schedule(
        schedules,
        tenant_id=_TENANT,
        owner_id="u1",
        name="x",
        cron_expr="0 9 * * *",
        task=_task(),
    )
    assert (
        schedules.advance(
            _TENANT,
            row.id,
            expected_next_fire_at=datetime(2030, 1, 1, tzinfo=UTC),
            next_fire_at=datetime(2030, 1, 2, tzinfo=UTC),
            last_fire_at=None,
        )
        is None
    )
    assert schedules.get(_TENANT, row.id).next_fire_at == row.next_fire_at


# --- worker scheduler pass --------------------------------------------------- #


class _FakeHandle:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id


async def _ok_run(task, ctx):
    return _FakeHandle("run-ok")


async def test_worker_fires_schedule_and_runs_it_in_the_same_pass(schedules, queue):
    now = _now()
    create_schedule(
        schedules,
        tenant_id=_TENANT,
        owner_id="u1",
        name="minutely",
        cron_expr="* * * * *",
        timezone="UTC",
        task=_task(),
        now=now - timedelta(minutes=1),
    )
    worker = QueueWorker(store=queue, schedule_store=schedules, run_task_fn=_ok_run)
    report = await worker.run_once()
    # The scheduler fired first, so the queue pass claimed the fired
    # row(s) in the same pass. Exactly one fire is due at `now` (the
    # count is tied to `done` rather than pinned to 1, so a minute
    # boundary crossed between create and run_once cannot flake it).
    fired = worker.scheduler_passes[-1].fired
    assert fired >= 1 and worker.scheduler_passes[-1].schedules == 1
    assert report.claimed == fired and report.done == fired
    assert queue.list_tasks(_TENANT, status="done")[0].run_id == "run-ok"


async def test_worker_scheduler_failure_does_not_kill_queue_pass(schedules, queue):
    from forgeflow.queue.service import enqueue_task

    enqueue_task(queue, tenant_id=_TENANT, owner_id="u1", task=_task(), idempotency_key="k1")

    class _BrokenScheduleStore:
        def list_due(self, *args, **kwargs):
            raise RuntimeError("schedule store down")

    worker = QueueWorker(
        store=queue, schedule_store=_BrokenScheduleStore(), run_task_fn=_ok_run
    )
    report = await worker.run_once()
    assert worker.scheduler_passes[-1].to_dict() == {"fired": 0, "schedules": 0}
    assert report.claimed == 1 and report.done == 1


# --- API --------------------------------------------------------------------- #


def _api_app(schedule_store, queue_store, tenant_override: str | None = None):
    from forgeflow.api.routers import queue as queue_router

    set_task_queue_store(queue_store)
    set_schedule_store(schedule_store)
    app = FastAPI()
    authenticate_business_app(app)
    if tenant_override is not None:
        app.dependency_overrides[resolve_tenant] = lambda: tenant_override
    app.include_router(queue_router.router, prefix="/queue")
    return app


def test_api_schedule_crud(schedules, queue):
    client = TestClient(_api_app(schedules, queue))
    resp = client.post(
        "/queue/schedules",
        json={
            "name": "morning",
            "cron_expr": "0 9 * * *",
            "timezone": "UTC",
            "intent": "每日汇总",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "morning" and body["enabled"] is True
    assert body["next_fire_at"] is not None and body["last_fire_at"] is None
    schedule_id = body["id"]

    listed = client.get("/queue/schedules")
    assert listed.status_code == 200 and [r["id"] for r in listed.json()] == [schedule_id]

    got = client.get(f"/queue/schedules/{schedule_id}")
    assert got.status_code == 200 and got.json()["id"] == schedule_id
    assert client.get("/queue/schedules/no-such-id").status_code == 404

    # Task-only patch leaves next_fire_at untouched.
    before = got.json()["next_fire_at"]
    patched = client.patch(
        f"/queue/schedules/{schedule_id}", json={"title": "new title", "enabled": False}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["next_fire_at"] == before
    assert patched.json()["enabled"] is False
    assert patched.json()["task"]["title"] == "new title"

    # Cron change recomputes next_fire_at.
    patched = client.patch(
        f"/queue/schedules/{schedule_id}", json={"cron_expr": "0 10 * * *", "enabled": True}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["next_fire_at"] != before

    deleted = client.delete(f"/queue/schedules/{schedule_id}")
    assert deleted.status_code == 204
    assert client.get(f"/queue/schedules/{schedule_id}").status_code == 404
    assert client.delete(f"/queue/schedules/{schedule_id}").status_code == 404


def test_api_schedule_rejects_bad_cron_and_timezone(schedules, queue):
    client = TestClient(_api_app(schedules, queue))
    assert (
        client.post(
            "/queue/schedules",
            json={"name": "x", "cron_expr": "bogus", "intent": "y"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/queue/schedules",
            json={"name": "x", "cron_expr": "0 9 * * *", "timezone": "Nope/Zone", "intent": "y"},
        ).status_code
        == 422
    )
    created = client.post(
        "/queue/schedules",
        json={"name": "x", "cron_expr": "0 9 * * *", "intent": "y"},
    )
    schedule_id = created.json()["id"]
    assert (
        client.patch(f"/queue/schedules/{schedule_id}", json={"cron_expr": "@daily"}).status_code
        == 422
    )


def test_api_schedule_is_tenant_scoped(schedules, queue):
    client = TestClient(_api_app(schedules, queue))
    created = client.post(
        "/queue/schedules",
        json={"name": "x", "cron_expr": "0 9 * * *", "intent": "y"},
    )
    schedule_id = created.json()["id"]
    other = TestClient(_api_app(schedules, queue, tenant_override=_OTHER))
    assert other.get(f"/queue/schedules/{schedule_id}").status_code == 404
    assert other.get("/queue/schedules").json() == []
    assert other.patch(f"/queue/schedules/{schedule_id}", json={"enabled": False}).status_code == 404
    assert other.delete(f"/queue/schedules/{schedule_id}").status_code == 404


def test_schedule_routes_are_rbac_mapped():
    assert RBACMiddleware._resolve_permission("POST", "/queue/schedules") == (
        "execute",
        "workflows",
    )
    assert RBACMiddleware._resolve_permission("GET", "/queue/schedules") == ("read", "workflows")
    assert RBACMiddleware._resolve_permission("GET", "/queue/schedules/some-id") == (
        "read",
        "workflows",
    )
    assert RBACMiddleware._resolve_permission("PATCH", "/queue/schedules/some-id") == (
        "execute",
        "workflows",
    )
    assert RBACMiddleware._resolve_permission("DELETE", "/queue/schedules/some-id") == (
        "execute",
        "workflows",
    )


def test_get_schedule_store_selector(monkeypatch):
    # conftest sets a sync DSN default, so the selector must be
    # tested with the env controlled, not assumed.
    monkeypatch.delenv("POSTGRES_SYNC_URL", raising=False)
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    reset_schedule_store()
    assert get_schedule_store().durability == "ephemeral"
    monkeypatch.setenv("POSTGRES_SYNC_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    reset_schedule_store()
    assert get_schedule_store().durability == "durable"
