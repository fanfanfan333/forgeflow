"""047 — schedules: cron schedules that enqueue queued tasks (A5 slice 2).

Creates the ``schedules`` table backing :mod:`forgeflow.queue.schedules`:
one row per cron schedule. A scheduler pass (inside
:class:`forgeflow.queue.worker.QueueWorker`) finds enabled rows whose
``next_fire_at`` has passed and enqueues one ``task_queue`` row per
fire time, using the deterministic idempotency key
``schedule:<schedule_id>:<fire_time_iso>`` — a double-fire is a
no-op through the queue's ``UNIQUE (tenant_id, idempotency_key)``,
and ``next_fire_at`` advances only when the row still carries the
fire time the worker started from, so two workers racing the same
schedule cannot advance past a fire.

``next_fire_at`` is stored in UTC; ``timezone`` is the IANA name the
cron expression is evaluated in (``croniter`` + ``zoneinfo``).

Deliberately NO RLS (the 045/046 precedent): every store query
scopes by ``tenant_id`` explicitly; the scheduler's due-scan is a
cross-tenant platform-service read, like the queue worker's claim.

Revision ID: 047
Revises: 046
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "047"
down_revision: str | Sequence[str] | None = "046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS schedules (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id        TEXT NOT NULL,
            owner_id         TEXT NOT NULL,
            name             TEXT NOT NULL,
            cron_expr        TEXT NOT NULL,
            timezone         TEXT NOT NULL DEFAULT 'UTC',
            task             JSONB NOT NULL,
            enabled          BOOLEAN NOT NULL DEFAULT true,
            next_fire_at     TIMESTAMPTZ NOT NULL,
            last_fire_at     TIMESTAMPTZ,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_schedules_due ON schedules (enabled, next_fire_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS schedules")
