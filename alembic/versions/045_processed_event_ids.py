"""045 — processed_event_ids: replay protection for signed events.

Creates the ``processed_event_ids`` table backing
:mod:`forgeflow.events.processed_events`: the events dispatcher
claims an event's id here before dispatching it, and a second claim
of the same id is rejected as a replay.  Signatures (HMAC over the
envelope, including the event id and its expiry) stop forgery; this
table stops a captured, valid envelope from being re-delivered
verbatim.

Deliberately NOT tenant-scoped and NOT under RLS (unlike 043/044):
the dedupe keyspace is global — an id replayed under a different
declared tenant must still be caught — and a row carries no tenant
data: the id itself, an optional audit-only tenant label, and
timestamps.  ``expires_at`` is the claim's retention horizon (the
event's own expiry plus grace); once it lapses the row may be
reclaimed, because by then the envelope itself fails the dispatcher's
expiry check anyway.

Revision ID: 045
Revises: 044
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "045"
down_revision: str | Sequence[str] | None = "044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_event_ids (
            event_id     TEXT PRIMARY KEY,
            tenant_id    TEXT,
            processed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at   TIMESTAMPTZ NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS processed_event_ids")
