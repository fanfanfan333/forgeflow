"""A5 slice 3 — inbound webhooks: store, signing, trigger flow, API.

Unit level: the InMemory stores (the Postgres store is covered by
tests/integration/test_webhooks_pg.py). The trigger's HMAC is the
dispatcher's canonical scheme keyed by the webhook's own secret;
the replay claim runs against an in-memory processed-event store.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.routers import webhooks as webhooks_router
from forgeflow.events.processed_events import (
    InMemoryProcessedEventStore,
    reset_processed_event_store,
    set_processed_event_store,
)
from forgeflow.middleware.auth import RBACMiddleware
from forgeflow.queue.store import (
    InMemoryTaskQueueStore,
    reset_task_queue_store,
    set_task_queue_store,
)
from forgeflow.queue.webhooks import (
    InMemoryWebhookStore,
    WebhookAuthError,
    WebhookNotFound,
    WebhookReplayError,
    create_webhook,
    get_webhook_store,
    reset_webhook_store,
    set_webhook_store,
    sign_trigger,
    trigger_webhook,
    verify_trigger,
)
from forgeflow.rbac.policies import ROUTE_PERMISSION_MAP
from tests.business_auth import authenticate_business_app

_TENANT = "t-webhook-unit"
_OTHER = "t-webhook-other"


def _now() -> datetime:
    return datetime.now(UTC)


def _future_iso(minutes: int = 30) -> str:
    return (_now() + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _past_iso(minutes: int = 5) -> str:
    return (_now() - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _template(**overrides):
    payload = {
        "intent": "生成周报",
        "title": "weekly",
        "workflow_type": "generic",
        "context": {"source": "webhook"},
    }
    payload.update(overrides)
    return payload


@pytest.fixture(autouse=True)
def _clean_stores():
    reset_webhook_store()
    reset_task_queue_store()
    reset_processed_event_store()
    yield
    reset_webhook_store()
    reset_task_queue_store()
    reset_processed_event_store()


@pytest.fixture()
def webhook_store():
    return InMemoryWebhookStore()


@pytest.fixture()
def queue_store():
    return InMemoryTaskQueueStore()


@pytest.fixture()
def processed_store():
    return InMemoryProcessedEventStore()


@pytest.fixture()
def webhook(webhook_store):
    row, secret = create_webhook(
        webhook_store,
        tenant_id=_TENANT,
        owner_id="owner-9",
        name="weekly-report",
        task_template=_template(),
    )
    return row, secret


def _signed(webhook, secret, *, task=None, event_id="evt-1", expires_at=None):
    expires = expires_at if expires_at is not None else _future_iso()
    return {
        "task": task,
        "event_id": event_id,
        "expires_at": expires,
        "signature": sign_trigger(
            secret,
            tenant_id=webhook.tenant_id,
            owner_id=webhook.owner_id,
            task=task,
            event_id=event_id,
            expires_at=expires,
        ),
    }


# --- signing --------------------------------------------------------------- #


def test_sign_verify_round_trip(webhook):
    row, secret = webhook
    body = _signed(row, secret, task={"intent": "生成周报"})
    assert verify_trigger(
        secret,
        tenant_id=row.tenant_id,
        owner_id=row.owner_id,
        task=body["task"],
        event_id=body["event_id"],
        expires_at=body["expires_at"],
        signature=body["signature"],
    )


def test_verify_rejects_wrong_secret_and_tampering(webhook):
    row, secret = webhook
    body = _signed(row, secret, task={"intent": "生成周报"})
    # Wrong key.
    assert not verify_trigger(
        "not-the-secret",
        tenant_id=row.tenant_id,
        owner_id=row.owner_id,
        task=body["task"],
        event_id=body["event_id"],
        expires_at=body["expires_at"],
        signature=body["signature"],
    )
    # Tampered payload.
    assert not verify_trigger(
        secret,
        tenant_id=row.tenant_id,
        owner_id=row.owner_id,
        task={"intent": "生成月报"},
        event_id=body["event_id"],
        expires_at=body["expires_at"],
        signature=body["signature"],
    )
    # Tampered event id / expiry (both covered verbatim).
    assert not verify_trigger(
        secret,
        tenant_id=row.tenant_id,
        owner_id=row.owner_id,
        task=body["task"],
        event_id="evt-2",
        expires_at=body["expires_at"],
        signature=body["signature"],
    )
    assert not verify_trigger(
        secret,
        tenant_id=row.tenant_id,
        owner_id=row.owner_id,
        task=body["task"],
        event_id=body["event_id"],
        expires_at=_future_iso(minutes=31),
        signature=body["signature"],
    )


def test_verify_rejects_expired_missing_and_empty_signature(webhook):
    row, secret = webhook
    body = _signed(row, secret, expires_at=_past_iso())
    assert not verify_trigger(
        secret,
        tenant_id=row.tenant_id,
        owner_id=row.owner_id,
        task=body["task"],
        event_id=body["event_id"],
        expires_at=body["expires_at"],
        signature=body["signature"],
    )
    good = _signed(row, secret)
    assert not verify_trigger(
        secret,
        tenant_id=row.tenant_id,
        owner_id=row.owner_id,
        task=good["task"],
        event_id=None,
        expires_at=good["expires_at"],
        signature=good["signature"],
    )
    assert not verify_trigger(
        secret,
        tenant_id=row.tenant_id,
        owner_id=row.owner_id,
        task=good["task"],
        event_id=good["event_id"],
        expires_at=good["expires_at"],
        signature="",
    )
    assert not verify_trigger(
        secret,
        tenant_id=row.tenant_id,
        owner_id=row.owner_id,
        task=good["task"],
        event_id=good["event_id"],
        expires_at="not-a-date",
        signature=good["signature"],
    )


# --- store: secret once, fingerprint only, tenant scoping ------------------- #


def test_create_returns_secret_once_and_views_hide_it(webhook_store):
    row, secret = create_webhook(
        webhook_store,
        tenant_id=_TENANT,
        owner_id="owner-9",
        name="w1",
        task_template=_template(),
    )
    assert secret and len(secret) > 20
    assert "secret" not in row.to_dict()
    assert len(row.secret_fingerprint) == 8
    assert all(c in "0123456789abcdef" for c in row.secret_fingerprint)
    listed = webhook_store.list_webhooks(_TENANT)
    assert len(listed) == 1 and "secret" not in listed[0].to_dict()
    got = webhook_store.get(_TENANT, row.id)
    assert got is not None and "secret" not in got.to_dict()
    assert got.secret_fingerprint == row.secret_fingerprint


def test_store_is_tenant_scoped(webhook_store):
    row, _secret = create_webhook(
        webhook_store,
        tenant_id=_TENANT,
        owner_id="owner-9",
        name="w1",
        task_template=_template(),
    )
    assert webhook_store.get(_OTHER, row.id) is None
    assert webhook_store.list_webhooks(_OTHER) == []
    assert webhook_store.delete(_OTHER, row.id) is False
    assert webhook_store.get(_TENANT, row.id) is not None
    assert webhook_store.delete(_TENANT, row.id) is True
    assert webhook_store.get(_TENANT, row.id) is None


def test_create_validates_name_and_template(webhook_store):
    with pytest.raises(ValueError):
        create_webhook(
            webhook_store, tenant_id=_TENANT, owner_id="o", name="  ",
            task_template=_template(),
        )
    with pytest.raises(ValueError):
        create_webhook(
            webhook_store, tenant_id=_TENANT, owner_id="o", name="w",
            task_template={"intent": ""},
        )


# --- trigger flow ----------------------------------------------------------- #


def test_trigger_enqueues_merged_task(
    webhook, webhook_store, queue_store, processed_store
):
    row, secret = webhook
    body = _signed(
        row,
        secret,
        task={"title": "override", "context": {"extra": "1"}},
        event_id="evt-merge",
    )
    queued = trigger_webhook(
        webhook_store,
        queue_store,
        processed_store,
        webhook_id=row.id,
        task=body["task"],
        event_id=body["event_id"],
        expires_at=body["expires_at"],
        signature=body["signature"],
    )
    assert queued.status == "queued"
    assert queued.tenant_id == _TENANT
    assert queued.owner_id == "owner-9"
    assert queued.idempotency_key == f"webhook:{row.id}:evt-merge"
    # Template intent survives; body title wins; contexts extend.
    assert queued.task["intent"] == "生成周报"
    assert queued.task["title"] == "override"
    assert queued.task["context"] == {"source": "webhook", "extra": "1"}


def test_trigger_without_body_task_uses_template(
    webhook, webhook_store, queue_store, processed_store
):
    row, secret = webhook
    body = _signed(row, secret, task=None, event_id="evt-plain")
    queued = trigger_webhook(
        webhook_store,
        queue_store,
        processed_store,
        webhook_id=row.id,
        task=body["task"],
        event_id=body["event_id"],
        expires_at=body["expires_at"],
        signature=body["signature"],
    )
    assert queued.task == _template()


def test_trigger_rejects_unknown_disabled_bad_and_expired(
    webhook, webhook_store, queue_store, processed_store
):
    row, secret = webhook
    good = _signed(row, secret, event_id="evt-ok")
    with pytest.raises(WebhookNotFound):
        trigger_webhook(
            webhook_store, queue_store, processed_store,
            webhook_id="no-such-id", task=good["task"], event_id=good["event_id"],
            expires_at=good["expires_at"], signature=good["signature"],
        )
    disabled, dsecret = create_webhook(
        webhook_store,
        tenant_id=_TENANT,
        owner_id="owner-9",
        name="off",
        task_template=_template(),
        enabled=False,
    )
    dbody = _signed(disabled, dsecret, event_id="evt-off")
    with pytest.raises(WebhookNotFound):
        trigger_webhook(
            webhook_store, queue_store, processed_store,
            webhook_id=disabled.id, task=dbody["task"], event_id=dbody["event_id"],
            expires_at=dbody["expires_at"], signature=dbody["signature"],
        )
    with pytest.raises(WebhookAuthError):
        trigger_webhook(
            webhook_store, queue_store, processed_store,
            webhook_id=row.id, task=good["task"], event_id=good["event_id"],
            expires_at=good["expires_at"], signature="deadbeef",
        )
    expired = _signed(row, secret, event_id="evt-old", expires_at=_past_iso())
    with pytest.raises(WebhookAuthError):
        trigger_webhook(
            webhook_store, queue_store, processed_store,
            webhook_id=row.id, task=expired["task"], event_id=expired["event_id"],
            expires_at=expired["expires_at"], signature=expired["signature"],
        )


def test_trigger_replay_is_rejected_and_queue_stays_single(
    webhook, webhook_store, queue_store, processed_store
):
    row, secret = webhook
    body = _signed(row, secret, event_id="evt-once")
    first = trigger_webhook(
        webhook_store, queue_store, processed_store,
        webhook_id=row.id, task=body["task"], event_id=body["event_id"],
        expires_at=body["expires_at"], signature=body["signature"],
    )
    with pytest.raises(WebhookReplayError):
        trigger_webhook(
            webhook_store, queue_store, processed_store,
            webhook_id=row.id, task=body["task"], event_id=body["event_id"],
            expires_at=body["expires_at"], signature=body["signature"],
        )
    rows = queue_store.list_tasks(_TENANT)
    assert [r.id for r in rows] == [first.id]


def test_trigger_rejected_forgery_does_not_burn_the_event_id(
    webhook, webhook_store, queue_store, processed_store
):
    row, secret = webhook
    body = _signed(row, secret, event_id="evt-legit")
    # A forgery with the same event id is rejected at the trust gate.
    with pytest.raises(WebhookAuthError):
        trigger_webhook(
            webhook_store, queue_store, processed_store,
            webhook_id=row.id, task=body["task"], event_id=body["event_id"],
            expires_at=body["expires_at"], signature="deadbeef",
        )
    # The legitimate trigger with the same id still goes through.
    queued = trigger_webhook(
        webhook_store, queue_store, processed_store,
        webhook_id=row.id, task=body["task"], event_id=body["event_id"],
        expires_at=body["expires_at"], signature=body["signature"],
    )
    assert queued.status == "queued"


def test_get_webhook_store_selector(monkeypatch):
    # conftest sets a sync DSN default, so the selector must be
    # tested with the env controlled, not assumed.
    monkeypatch.delenv("POSTGRES_SYNC_URL", raising=False)
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    reset_webhook_store()
    assert get_webhook_store().durability == "ephemeral"
    monkeypatch.setenv("POSTGRES_SYNC_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    reset_webhook_store()
    assert get_webhook_store().durability == "durable"


# --- API ------------------------------------------------------------------- #


def _mgmt_app(webhook_store, tenant_override: str | None = None):
    set_webhook_store(webhook_store)
    app = FastAPI()
    authenticate_business_app(app)
    if tenant_override is not None:
        app.dependency_overrides[resolve_tenant] = lambda: tenant_override
    app.include_router(webhooks_router.router, prefix="/queue")
    return app


def _trigger_app(webhook_store, queue_store, processed_store):
    set_webhook_store(webhook_store)
    set_task_queue_store(queue_store)
    set_processed_event_store(processed_store)
    app = FastAPI()
    app.add_middleware(RBACMiddleware)
    app.include_router(webhooks_router.trigger_router, prefix="/webhooks")
    return app


def test_api_management_crud_and_secret_once(webhook_store):
    client = TestClient(_mgmt_app(webhook_store))
    created = client.post(
        "/queue/webhooks",
        json={"name": "w1", "intent": "生成周报", "context": {"source": "webhook"}},
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["name"] == "w1" and body["enabled"] is True
    assert body["secret"] and len(body["secret"]) > 20
    assert len(body["secret_fingerprint"]) == 8
    webhook_id = body["id"]

    listed = client.get("/queue/webhooks")
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert "secret" not in listed.json()[0]

    got = client.get(f"/queue/webhooks/{webhook_id}")
    assert got.status_code == 200 and "secret" not in got.json()
    assert client.get("/queue/webhooks/no-such-id").status_code == 404

    deleted = client.delete(f"/queue/webhooks/{webhook_id}")
    assert deleted.status_code == 204
    assert client.get(f"/queue/webhooks/{webhook_id}").status_code == 404
    assert client.delete(f"/queue/webhooks/{webhook_id}").status_code == 404


def test_api_management_is_tenant_scoped(webhook_store):
    client = TestClient(_mgmt_app(webhook_store))
    created = client.post("/queue/webhooks", json={"name": "w1", "intent": "x"})
    webhook_id = created.json()["id"]
    other = TestClient(_mgmt_app(webhook_store, tenant_override=_OTHER))
    assert other.get(f"/queue/webhooks/{webhook_id}").status_code == 404
    assert other.get("/queue/webhooks").json() == []
    assert other.delete(f"/queue/webhooks/{webhook_id}").status_code == 404


def test_api_trigger_no_auth_header_end_to_end(
    webhook, webhook_store, queue_store, processed_store
):
    row, secret = webhook
    client = TestClient(
        _trigger_app(webhook_store, queue_store, processed_store)
    )
    body = _signed(row, secret, task={"title": "via-http"}, event_id="evt-http")
    # No Authorization header at all: the HMAC is the credential.
    resp = client.post(f"/webhooks/{row.id}/trigger", json=body)
    assert resp.status_code == 202, resp.text
    data = resp.json()
    assert data["webhook_id"] == row.id and data["status"] == "queued"
    assert queue_store.list_tasks(_TENANT)[0].id == data["queued_task_id"]

    # Same event id again: replay, 409 — and still exactly one queue row.
    again = client.post(f"/webhooks/{row.id}/trigger", json=body)
    assert again.status_code == 409, again.text
    assert len(queue_store.list_tasks(_TENANT)) == 1


def test_api_trigger_rejects_bad_unknown_disabled_expired(
    webhook, webhook_store, queue_store, processed_store
):
    row, secret = webhook
    client = TestClient(
        _trigger_app(webhook_store, queue_store, processed_store)
    )
    good = _signed(row, secret, event_id="evt-good")
    bad = dict(good, signature="deadbeef")
    assert client.post(f"/webhooks/{row.id}/trigger", json=bad).status_code == 403
    assert client.post("/webhooks/no-such-id/trigger", json=good).status_code == 404
    expired = _signed(row, secret, event_id="evt-exp", expires_at=_past_iso())
    assert client.post(f"/webhooks/{row.id}/trigger", json=expired).status_code == 403

    disabled, dsecret = create_webhook(
        webhook_store,
        tenant_id=_TENANT,
        owner_id="owner-9",
        name="off",
        task_template=_template(),
        enabled=False,
    )
    dbody = _signed(disabled, dsecret, event_id="evt-off")
    assert client.post(f"/webhooks/{disabled.id}/trigger", json=dbody).status_code == 404


def test_trigger_route_is_open_and_management_routes_are_mapped():
    assert RBACMiddleware._is_open("/webhooks/some-id/trigger") is True
    assert RBACMiddleware._is_open("/queue/webhooks") is False
    assert ROUTE_PERMISSION_MAP[("POST", "/queue/webhooks")] == ("execute", "workflows")
    assert ROUTE_PERMISSION_MAP[("GET", "/queue/webhooks")] == ("read", "workflows")
    assert ROUTE_PERMISSION_MAP[("DELETE", "/queue/webhooks")] == ("execute", "workflows")
    # The trigger route deliberately has no map entry: a JWT must not
    # be required — the HMAC is the credential.
    assert ("POST", "/webhooks") not in ROUTE_PERMISSION_MAP
