"""049 — worker_heartbeats: liveness for standalone queue workers (A5 slice 4).

Creates the ``worker_heartbeats`` table backing
:mod:`forgeflow.queue.heartbeats`: one row per worker process
(``worker_id``), upserted once per poll interval by
:class:`forgeflow.queue.worker.QueueWorker` and once at startup.
Operators read liveness from ``GET /queue/workers``: a row whose
``last_beat_at`` is older than 3x its poll interval (recorded in
``last_report``) is stale — the worker is down.

Deliberately NO RLS (the 045/046/047/048 precedent): heartbeats
are platform-wide (a worker serves all tenants, like the claim
operation), every store query is an explicit platform query, and
there is no tenant to scope by.

Revision ID: 049
Revises: 048
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "049"
down_revision: str | Sequence[str] | None = "048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS worker_heartbeats (
            worker_id    TEXT PRIMARY KEY,
            hostname     TEXT,
            started_at   TIMESTAMPTZ NOT NULL,
            last_beat_at TIMESTAMPTZ NOT NULL,
            last_report  JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_worker_heartbeats_beat "
        "ON worker_heartbeats (last_beat_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS worker_heartbeats")
