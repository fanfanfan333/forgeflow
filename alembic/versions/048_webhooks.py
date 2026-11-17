"""048 — webhooks: inbound webhook endpoints that enqueue runs (A5 slice 3).

Creates the ``webhooks`` table backing :mod:`forgeflow.queue.webhooks`:
one row per inbound webhook. An external producer calls
``POST /webhooks/{id}/trigger`` with an HMAC-SHA256 signature over
the trigger body (the dispatcher's canonical scheme, keyed by this
webhook's own secret); a valid, fresh, never-before-seen trigger
enqueues the webhook's task template (merged with the body's task
override) into the durable queue under the deterministic idempotency
key ``webhook:<webhook_id>:<event_id>``.

Secret storage (deliberate): HMAC verification needs the signing
key itself, so the per-webhook secret is stored in the ``secret``
column — the DB is the platform's trusted store, the same trust as
other platform-held credentials. The secret is generated at creation,
returned exactly once in the create response, and never returned by
list/get (only a short sha256 fingerprint), never logged.

Deliberately NO RLS (the 045/046/047 precedent): every store query
scopes by ``tenant_id`` explicitly; the trigger lookup is by id only
because the trigger authenticates by signature, not by tenant
session, and the signature is the credential.

Revision ID: 048
Revises: 047
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "048"
down_revision: str | Sequence[str] | None = "047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS webhooks (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id        TEXT NOT NULL,
            owner_id         TEXT NOT NULL,
            name             TEXT NOT NULL,
            secret           TEXT NOT NULL,
            task_template    JSONB NOT NULL,
            enabled          BOOLEAN NOT NULL DEFAULT true,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_webhooks_tenant ON webhooks (tenant_id, enabled)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS webhooks")
