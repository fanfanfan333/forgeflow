"""A5 slice 3: Postgres webhook store + trigger flow against the real
``webhooks`` table.

Skips when live Postgres is unreachable or migration 048 has not
been applied (the migration's own application is verified by the
alembic upgrade in the verification run, not by this test).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from forgeflow.events.processed_events import PostgresProcessedEventStore
from forgeflow.queue.store import PostgresTaskQueueStore
from forgeflow.queue.webhooks import (
    PostgresWebhookStore,
    WebhookAuthError,
    WebhookNotFound,
    WebhookReplayError,
    create_webhook,
    sign_trigger,
    trigger_webhook,
)

_TENANT = "t-webhook-pg"
_OTHER = "t-webhook-pg-other"


def _dsn():
    dsn = os.environ.get("POSTGRES_SYNC_URL") or os.environ.get("POSTGRES_DSN") or ""
    return dsn.replace("postgresql+psycopg://", "postgresql://")


def _future_iso(minutes: int = 30) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


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
            cur.execute("SELECT 1 FROM webhooks LIMIT 1")
            cur.execute("SELECT 1 FROM task_queue LIMIT 1")
            cur.execute("SELECT 1 FROM processed_event_ids LIMIT 1")
    except Exception:  # noqa: BLE001
        conn.close()
        pytest.skip("webhooks/task_queue/processed_event_ids table missing (migration 048 not applied)")
    conn.close()
    hooks = PostgresWebhookStore(dsn)
    queue = PostgresTaskQueueStore(dsn)
    processed = PostgresProcessedEventStore(dsn)
    yield hooks, queue, processed
    try:
        conn = psycopg.connect(dsn)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM webhooks WHERE tenant_id IN (%s, %s)", (_TENANT, _OTHER))
            cur.execute("DELETE FROM task_queue WHERE tenant_id IN (%s, %s)", (_TENANT, _OTHER))
            cur.execute("DELETE FROM processed_event_ids WHERE tenant_id IN (%s, %s)", (_TENANT, _OTHER))
        conn.commit()
        conn.close()
    except Exception:  # noqa: BLE001 — test cleanup must not fail the test
        pass


def _template(**overrides):
    payload = {
        "intent": "生成周报",
        "title": "weekly",
        "workflow_type": "generic",
        "context": {"source": "webhook"},
    }
    payload.update(overrides)
    return payload


def test_pg_create_trigger_replay(stores):
    hooks, queue, processed = stores
    row, secret = create_webhook(
        hooks,
        tenant_id=_TENANT,
        owner_id="owner-pg",
        name="pg-hook",
        task_template=_template(),
    )
    assert row.id and secret
    # Read model hides the secret; the fingerprint is stable.
    assert "secret" not in row.to_dict()
    again = hooks.get(_TENANT, row.id)
    assert again is not None and again.secret_fingerprint == row.secret_fingerprint
    assert hooks.get(_OTHER, row.id) is None

    expires = _future_iso()
    signature = sign_trigger(
        secret,
        tenant_id=_TENANT,
        owner_id="owner-pg",
        task={"title": "pg-override"},
        event_id="evt-pg-1",
        expires_at=expires,
    )
    queued = trigger_webhook(
        hooks, queue, processed,
        webhook_id=row.id,
        task={"title": "pg-override"},
        event_id="evt-pg-1",
        expires_at=expires,
        signature=signature,
    )
    assert queued.status == "queued"
    assert queued.tenant_id == _TENANT
    assert queued.idempotency_key == f"webhook:{row.id}:evt-pg-1"
    assert queued.task["title"] == "pg-override"
    assert queued.task["intent"] == "生成周报"

    # The same event id is a replay — even with a fresh signature.
    # (The expiry string is captured once: it is covered verbatim
    # by the signature, so signing and sending must use the same one.)
    expires2 = _future_iso(minutes=60)
    signature2 = sign_trigger(
        secret,
        tenant_id=_TENANT,
        owner_id="owner-pg",
        task={"title": "pg-override"},
        event_id="evt-pg-1",
        expires_at=expires2,
    )
    with pytest.raises(WebhookReplayError):
        trigger_webhook(
            hooks, queue, processed,
            webhook_id=row.id,
            task={"title": "pg-override"},
            event_id="evt-pg-1",
            expires_at=expires2,
            signature=signature2,
        )
    assert len(queue.list_tasks(_TENANT)) == 1


def test_pg_trigger_rejects_bad_signature_and_unknown(stores):
    hooks, queue, processed = stores
    row, secret = create_webhook(
        hooks,
        tenant_id=_TENANT,
        owner_id="owner-pg",
        name="pg-hook-2",
        task_template=_template(),
    )
    expires = _future_iso()
    with pytest.raises(WebhookAuthError):
        trigger_webhook(
            hooks, queue, processed,
            webhook_id=row.id,
            task=None,
            event_id="evt-pg-2",
            expires_at=expires,
            signature="deadbeef",
        )
    good = sign_trigger(
        secret,
        tenant_id=_TENANT,
        owner_id="owner-pg",
        task=None,
        event_id="evt-pg-2",
        expires_at=expires,
    )
    with pytest.raises(WebhookNotFound):
        trigger_webhook(
            hooks, queue, processed,
            webhook_id="00000000-0000-0000-0000-000000000000",
            task=None,
            event_id="evt-pg-2",
            expires_at=expires,
            signature=good,
        )
    # A rejected forgery did not burn the id: the good trigger works.
    queued = trigger_webhook(
        hooks, queue, processed,
        webhook_id=row.id,
        task=None,
        event_id="evt-pg-2",
        expires_at=expires,
        signature=good,
    )
    assert queued.task == _template()


def test_pg_webhook_crud(stores):
    hooks, _queue, _processed = stores
    row, _secret = create_webhook(
        hooks,
        tenant_id=_TENANT,
        owner_id="owner-pg",
        name="pg-crud",
        task_template=_template(),
    )
    assert [r.id for r in hooks.list_webhooks(_TENANT)] == [row.id]
    assert hooks.list_webhooks(_OTHER) == []
    assert hooks.delete(_OTHER, row.id) is False
    assert hooks.delete(_TENANT, row.id) is True
    assert hooks.get(_TENANT, row.id) is None
    assert hooks.get_for_trigger(row.id) is None


def test_pg_get_for_trigger_returns_secret(stores):
    hooks, _queue, _processed = stores
    row, secret = create_webhook(
        hooks,
        tenant_id=_TENANT,
        owner_id="owner-pg",
        name="pg-secret",
        task_template=_template(),
    )
    found = hooks.get_for_trigger(row.id)
    assert found is not None
    found_row, found_secret = found
    assert found_row.id == row.id
    assert found_secret == secret
