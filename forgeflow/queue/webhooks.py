"""Inbound webhooks (A5 slice 3).

One row per webhook: an external producer calls
``POST /webhooks/{id}/trigger`` with a JSON body ``{task, event_id,
expires_at, signature}``. The trigger is authenticated by an
HMAC-SHA256 signature — the dispatcher's canonical scheme
(:func:`forgeflow.events.dispatcher.sign_task_event`), keyed by this
webhook's own secret instead of the deployment's
``EVENT_SIGNING_SECRET``:

* the signed payload is the body task as received (``{}`` when the
  body carries no task override); the server-side template is trusted
  data and is merged in *after* verification, so the signature covers
  exactly what was transmitted;
* identity in the canonical bytes is server-resolved: ``tenant_id``
  and ``user_id`` come from the stored webhook row, ``role`` is the
  fixed ``"service"`` role (a producer asserts *who* calls, never a
  more privileged role — the dispatcher's rule);
* ``event_id`` / ``expires_at`` are covered verbatim, so a captured
  trigger cannot be re-labelled with a fresh id or a later expiry.

Freshness uses the dispatcher's rule (no clock-skew tolerance: an
expiry at or before now is rejected). Replay protection claims the
``event_id`` in the processed-event registry
(:mod:`forgeflow.events.processed_events`) *after* the trust gate —
a rejected forgery must not burn the legitimate event's id — with
retention ``expiry + CLAIM_EXPIRY_GRACE``, mirroring the dispatcher.
A successful trigger enqueues into the durable queue under the
deterministic idempotency key ``webhook:<webhook_id>:<event_id>``,
so a trigger that passed the claim but hit a transport retry is
still a single queued run.

The secret is generated at creation, returned exactly once, and
stored in the ``webhooks.secret`` column (HMAC verification needs
the key itself; see migration 048). The read model carries only a
short sha256 fingerprint. The raw secret is exposed to callers only
through :meth:`WebhookStore.get_for_trigger`, which the trigger
flow uses for verification and never logs or returns.

The store is sync by design, mirroring :mod:`forgeflow.queue.store`
and :mod:`forgeflow.queue.schedules`; async callers offload with
``asyncio.to_thread``. InMemory is "ephemeral" (tests / no-PG
runs); Postgres is "durable".
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from forgeflow.queue.service import enqueue_task, normalise_task_payload
from forgeflow.queue.store import QueuedTask

logger = logging.getLogger(__name__)

__all__ = [
    "Webhook",
    "WebhookAuthError",
    "WebhookNotFound",
    "WebhookReplayError",
    "WebhookUnavailableError",
    "InMemoryWebhookStore",
    "PostgresWebhookStore",
    "WebhookStore",
    "create_webhook",
    "get_webhook_store",
    "reset_webhook_store",
    "set_webhook_store",
    "sign_trigger",
    "trigger_webhook",
    "verify_trigger",
]

#: The role a triggered run executes under, resolved server-side.
#: A producer asserts *who* calls, never a more privileged role.
TRIGGER_ROLE = "service"

#: Retention grace added to a trigger's ``expires_at`` for the
#: replay claim — the dispatcher's ``CLAIM_EXPIRY_GRACE``.
CLAIM_GRACE = timedelta(hours=1)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _fingerprint(secret: str) -> str:
    """Short sha256 fingerprint of a secret (operator matching only)."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:8]


def _parse_expiry(expires_at: str | None) -> datetime | None:
    """Parse an ``expires_at`` to an aware datetime (None when
    missing/unparseable). Naive values are read as UTC — the
    dispatcher's rule."""
    if not expires_at:
        return None
    try:
        parsed = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class WebhookNotFound(Exception):
    """No such webhook, or a disabled one (indistinguishable by design)."""


class WebhookAuthError(Exception):
    """Bad/missing signature, or an expired/unparseable ``expires_at``."""


class WebhookReplayError(Exception):
    """The ``event_id`` was already claimed (a re-delivered trigger)."""


class WebhookUnavailableError(Exception):
    """The replay registry is unavailable — fail closed, do not trigger."""


@dataclass
class Webhook:
    """One inbound webhook. The read model never carries the secret."""

    id: str
    tenant_id: str
    owner_id: str
    name: str
    secret_fingerprint: str
    task_template: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
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
            "secret_fingerprint": self.secret_fingerprint,
            "task_template": dict(self.task_template or {}),
            "enabled": bool(self.enabled),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


class WebhookStore(Protocol):
    """The webhook contract both implementations satisfy (all sync)."""

    durability: str

    def create(
        self,
        tenant_id: str,
        *,
        owner_id: str,
        name: str,
        task_template: dict[str, Any],
        enabled: bool = True,
    ) -> tuple[Webhook, str]: ...

    def get(self, tenant_id: str, webhook_id: str) -> Webhook | None: ...

    def list_webhooks(
        self,
        tenant_id: str,
        *,
        limit: int = 20,
        enabled: bool | None = None,
    ) -> list[Webhook]: ...

    def delete(self, tenant_id: str, webhook_id: str) -> bool: ...

    def get_for_trigger(self, webhook_id: str) -> tuple[Webhook, str] | None: ...


def sign_trigger(
    secret: str,
    *,
    tenant_id: str,
    owner_id: str,
    task: dict[str, Any] | None,
    event_id: str,
    expires_at: str,
) -> str:
    """Produce the ``signature`` value for a trigger body (producer side).

    Reuses the dispatcher's canonical bytes: the signed payload is
    the body task as transmitted, the identity is the webhook's
    tenant/owner with the fixed ``"service"`` role, and ``event_id``
    / ``expires_at`` are covered verbatim.
    """
    from forgeflow.events.dispatcher import sign_task_event

    return sign_task_event(
        dict(task or {}),
        tenant_id=tenant_id,
        user_id=owner_id,
        role=TRIGGER_ROLE,
        secret=secret,
        event_id=event_id,
        expires_at=expires_at,
    )


def verify_trigger(
    secret: str,
    *,
    tenant_id: str,
    owner_id: str,
    task: dict[str, Any] | None,
    event_id: str | None,
    expires_at: str | None,
    signature: str | None,
    now: datetime | None = None,
) -> bool:
    """True iff the trigger carries a valid, fresh signature.

    Valid means: a non-empty signature matching the canonical bytes
    over the webhook's identity + the body task + ``event_id`` +
    ``expires_at``; an ``event_id`` is present (an unidentifiable
    trigger cannot be replay-checked); and ``expires_at`` parses and
    is still in the future. No clock-skew tolerance — the
    dispatcher's rule.
    """
    if not secret or not isinstance(signature, str) or not signature:
        return False
    if not event_id:
        return False
    expiry = _parse_expiry(expires_at)
    if expiry is None:
        return False
    if expiry <= (now or _utcnow()):
        return False
    expected = sign_trigger(
        secret,
        tenant_id=tenant_id,
        owner_id=owner_id,
        task=task,
        event_id=event_id,
        expires_at=str(expires_at),
    )
    return hmac.compare_digest(expected, signature)


def _merge_task(template: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    """Merge a body task override onto the webhook's template.

    Per-field: the body's ``intent`` / ``title`` / ``workflow_type``
    win when present, else the template's. ``context`` is extended:
    the body's keys win, the template's other keys survive.
    """
    body = dict(override or {})
    merged: dict[str, Any] = {
        "intent": body.get("intent") or template.get("intent", ""),
        "title": body.get("title", template.get("title", "")),
        "workflow_type": body.get("workflow_type") or template.get("workflow_type", "generic"),
        "context": {**dict(template.get("context") or {}), **dict(body.get("context") or {})},
    }
    return merged


class InMemoryWebhookStore:
    """Process-lifetime store (tests / no-PG runs)."""

    durability = "ephemeral"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rows: dict[str, Webhook] = {}
        self._secrets: dict[str, str] = {}
        self._seq = 0

    def create(
        self,
        tenant_id: str,
        *,
        owner_id: str,
        name: str,
        task_template: dict[str, Any],
        enabled: bool = True,
    ) -> tuple[Webhook, str]:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name is required (non-empty string)")
        payload = normalise_task_payload(task_template)
        secret = secrets.token_urlsafe(32)
        now = _utcnow()
        with self._lock:
            self._seq += 1
            row = Webhook(
                id=f"mem-{self._seq}-{uuid.uuid4().hex[:8]}",
                tenant_id=tenant_id,
                owner_id=owner_id,
                name=name.strip(),
                secret_fingerprint=_fingerprint(secret),
                task_template=payload,
                enabled=bool(enabled),
                created_at=now,
                updated_at=now,
            )
            self._rows[row.id] = row
            self._secrets[row.id] = secret
            return row, secret

    def get(self, tenant_id: str, webhook_id: str) -> Webhook | None:
        if not tenant_id or not webhook_id:
            return None
        with self._lock:
            row = self._rows.get(webhook_id)
            if row is None or row.tenant_id != tenant_id:
                return None
            return row

    def list_webhooks(
        self,
        tenant_id: str,
        *,
        limit: int = 20,
        enabled: bool | None = None,
    ) -> list[Webhook]:
        if not tenant_id:
            return []
        with self._lock:
            rows = [r for r in self._rows.values() if r.tenant_id == tenant_id]
            if enabled is not None:
                rows = [r for r in rows if r.enabled is bool(enabled)]
            rows.sort(key=lambda r: r.created_at or _utcnow(), reverse=True)
            return rows[: max(1, int(limit))]

    def delete(self, tenant_id: str, webhook_id: str) -> bool:
        with self._lock:
            row = self._rows.get(webhook_id)
            if row is None or row.tenant_id != tenant_id:
                return False
            del self._rows[webhook_id]
            self._secrets.pop(webhook_id, None)
            return True

    def get_for_trigger(self, webhook_id: str) -> tuple[Webhook, str] | None:
        if not webhook_id:
            return None
        with self._lock:
            row = self._rows.get(webhook_id)
            if row is None:
                return None
            return row, self._secrets[row.id]


def _normalise_dsn(dsn: str) -> str:
    return dsn.replace("postgresql+psycopg://", "postgresql://")


_COLUMNS = "id, tenant_id, owner_id, name, task_template, enabled, created_at, updated_at"
_COLUMN_NAMES = _COLUMNS.split(", ")


def _row_to_webhook(row: dict[str, Any], secret: str) -> Webhook:
    """Map a PG row (as a ``{column: value}`` dict) to a Webhook."""
    template = row.get("task_template")
    if isinstance(template, str):
        template = json.loads(template)
    return Webhook(
        id=str(row.get("id")),
        tenant_id=str(row.get("tenant_id")),
        owner_id=str(row.get("owner_id")),
        name=str(row.get("name") or ""),
        secret_fingerprint=_fingerprint(secret),
        task_template=dict(template or {}),
        enabled=bool(row.get("enabled")),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


class PostgresWebhookStore:
    """The real ``webhooks`` table (migration ``048``) via ``psycopg``."""

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
        task_template: dict[str, Any],
        enabled: bool = True,
    ) -> tuple[Webhook, str]:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name is required (non-empty string)")
        payload = normalise_task_payload(task_template)
        secret = secrets.token_urlsafe(32)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO webhooks "
                "(tenant_id, owner_id, name, secret, task_template, enabled) "
                "VALUES (%s, %s, %s, %s, %s::jsonb, %s) "
                f"RETURNING {_COLUMNS}",
                (
                    tenant_id,
                    owner_id,
                    name.strip(),
                    secret,
                    json.dumps(payload),
                    bool(enabled),
                ),
            )
            row = cur.fetchone()
            conn.commit()
        if row is None:  # pragma: no cover — an INSERT ... RETURNING always yields a row
            raise RuntimeError("webhooks insert returned no row")
        return _row_to_webhook(dict(zip(_COLUMN_NAMES, row, strict=True)), secret), secret

    def get(self, tenant_id: str, webhook_id: str) -> Webhook | None:
        if not tenant_id or not webhook_id:
            return None
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_COLUMNS}, secret FROM webhooks WHERE id = %s AND tenant_id = %s",
                (webhook_id, tenant_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        data = dict(zip(_COLUMN_NAMES + ["secret"], row, strict=True))
        return _row_to_webhook(data, str(data["secret"]))

    def list_webhooks(
        self,
        tenant_id: str,
        *,
        limit: int = 20,
        enabled: bool | None = None,
    ) -> list[Webhook]:
        if not tenant_id:
            return []
        with self._connect() as conn, conn.cursor() as cur:
            if enabled is not None:
                cur.execute(
                    f"SELECT {_COLUMNS}, secret FROM webhooks "
                    "WHERE tenant_id = %s AND enabled = %s "
                    "ORDER BY created_at DESC LIMIT %s",
                    (tenant_id, bool(enabled), max(1, int(limit))),
                )
            else:
                cur.execute(
                    f"SELECT {_COLUMNS}, secret FROM webhooks "
                    "WHERE tenant_id = %s ORDER BY created_at DESC LIMIT %s",
                    (tenant_id, max(1, int(limit))),
                )
            rows = cur.fetchall()
        out: list[Webhook] = []
        for row in rows:
            data = dict(zip(_COLUMN_NAMES + ["secret"], row, strict=True))
            out.append(_row_to_webhook(data, str(data["secret"])))
        return out

    def delete(self, tenant_id: str, webhook_id: str) -> bool:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM webhooks WHERE id = %s AND tenant_id = %s RETURNING id",
                (webhook_id, tenant_id),
            )
            deleted = cur.fetchone() is not None
            conn.commit()
        return deleted

    def get_for_trigger(self, webhook_id: str) -> tuple[Webhook, str] | None:
        if not webhook_id:
            return None
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_COLUMNS}, secret FROM webhooks WHERE id = %s",
                (webhook_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        data = dict(zip(_COLUMN_NAMES + ["secret"], row, strict=True))
        secret = str(data["secret"])
        return _row_to_webhook(data, secret), secret


def create_webhook(
    store: WebhookStore,
    *,
    tenant_id: str,
    owner_id: str,
    name: str,
    task_template: dict[str, Any],
    enabled: bool = True,
) -> tuple[Webhook, str]:
    """Validate a webhook and create it. Returns (webhook, raw secret).

    The raw secret is returned exactly once — the caller (the create
    endpoint) shows it in the create response and never again.
    """
    payload = normalise_task_payload(task_template)
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name is required (non-empty string)")
    return store.create(
        tenant_id,
        owner_id=owner_id,
        name=name.strip(),
        task_template=payload,
        enabled=bool(enabled),
    )


def trigger_webhook(
    webhook_store: WebhookStore,
    queue_store: Any,
    processed_store: Any,
    *,
    webhook_id: str,
    task: dict[str, Any] | None,
    event_id: str | None,
    expires_at: str | None,
    signature: str | None,
    now: datetime | None = None,
) -> QueuedTask:
    """Verify a trigger and enqueue the webhook's task.

    Order matters (the dispatcher's discipline): the webhook is
    looked up first (unknown/disabled → :class:`WebhookNotFound`,
    indistinguishable), then the signature + freshness are verified
    (:class:`WebhookAuthError`), then the merged task is validated,
    and only then is the ``event_id`` claimed in the processed-event
    registry (:class:`WebhookReplayError` on a duplicate). A rejected
    forgery never burns the legitimate event's id, and an expired
    trigger is rejected without being claimed — recording it would
    poison a later legitimate redelivery under the same id.

    Runs under ``tenant_scope(webhook.tenant_id)`` so durable
    writers inside the enqueue see the webhook's tenant.
    """
    at = now or _utcnow()
    found = webhook_store.get_for_trigger(webhook_id)
    if found is None:
        raise WebhookNotFound(f"webhook {webhook_id} not found")
    webhook, secret = found
    if not webhook.enabled:
        raise WebhookNotFound(f"webhook {webhook_id} not found")
    if not verify_trigger(
        secret,
        tenant_id=webhook.tenant_id,
        owner_id=webhook.owner_id,
        task=task,
        event_id=event_id,
        expires_at=expires_at,
        signature=signature,
        now=at,
    ):
        raise WebhookAuthError("trigger signature invalid or expired")
    merged = _merge_task(webhook.task_template, task)
    # Validate before claiming: a malformed task must not burn the id.
    normalise_task_payload(merged)
    expiry = _parse_expiry(expires_at)
    # verify_trigger already proved expiry parses and is in the future.
    retention_until = (expiry or at) + CLAIM_GRACE
    try:
        claimed = processed_store.claim(
            str(event_id), retention_until=retention_until, tenant_id=webhook.tenant_id
        )
    except Exception as exc:  # noqa: BLE001 — registry down ⇒ fail closed
        raise WebhookUnavailableError("replay registry unavailable; trigger refused") from exc
    if not claimed:
        raise WebhookReplayError(f"event {event_id} already processed")
    from forgeflow.tenant_scope import tenant_scope

    with tenant_scope(webhook.tenant_id):
        return enqueue_task(
            queue_store,
            tenant_id=webhook.tenant_id,
            owner_id=webhook.owner_id,
            task=merged,
            idempotency_key=f"webhook:{webhook.id}:{event_id}",
        )


_STORE: Any = None


def get_webhook_store() -> Any:
    """Process-wide store: PG when a sync DSN is set, else InMemory."""
    global _STORE
    if _STORE is None:
        dsn = os.environ.get("POSTGRES_SYNC_URL") or os.environ.get("POSTGRES_DSN")
        _STORE = PostgresWebhookStore(dsn) if dsn else InMemoryWebhookStore()
    return _STORE


def set_webhook_store(store: Any) -> None:
    global _STORE
    _STORE = store


def reset_webhook_store() -> None:
    global _STORE
    _STORE = None
