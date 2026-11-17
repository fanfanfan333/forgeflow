"""046 — task_queue: the durable generic task queue (A5 slice 1).

Creates the ``task_queue`` table backing :mod:`forgeflow.queue.store`:
one row per enqueued run request. A worker claims due rows
(``status`` in ``queued``/``failed`` with ``next_attempt_at`` past)
under a lease (``claimed_by`` + ``lease_expires_at``); a crash leaves
the lease to expire, after which the row is reaped back to ``queued``
— never silently lost, never double-executed while a lease is live.

Exactly-once at enqueue: ``UNIQUE (tenant_id, idempotency_key)`` —
a re-delivered enqueue returns the existing row instead of a second
queued run (the same discipline as the processed-event registry).

Deliberately NO RLS in this slice (the 045 precedent): the worker's
claim/reap are cross-tenant by design (a platform service identity),
so a tenant-equality policy would fight the design. Every store
query scopes by ``tenant_id`` explicitly; an RLS policy needs a
service-bypass decision first and is a documented follow-up.

Revision ID: 046
Revises: 045
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "046"
down_revision: str | Sequence[str] | None = "045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS task_queue (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id        TEXT NOT NULL,
            owner_id         TEXT NOT NULL,
            task             JSONB NOT NULL,
            idempotency_key  TEXT NOT NULL,
            status           TEXT NOT NULL DEFAULT 'queued'
                             CHECK (status IN ('queued', 'claimed', 'running',
                                              'done', 'failed', 'dead')),
            attempts         INT NOT NULL DEFAULT 0,
            max_attempts     INT NOT NULL DEFAULT 3,
            next_attempt_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            claimed_by       TEXT,
            lease_expires_at TIMESTAMPTZ,
            last_error       TEXT,
            run_id           TEXT,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, idempotency_key)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_task_queue_due "
        "ON task_queue (status, next_attempt_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS task_queue")
