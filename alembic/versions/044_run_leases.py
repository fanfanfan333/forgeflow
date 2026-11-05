"""044 — run_leases: one session lease per (tenant, run), with fencing.

Creates the ``run_leases`` table backing
:mod:`forgeflow.runtime.run_lease`: a worker must hold the lease before
executing or resuming a run, and ``fencing_token`` strictly increases on
every acquisition so a stale owner's late writes can be refused.
Release is a tombstone (``released``), never a delete, so the token
stays monotonic across release/re-acquire cycles too.  CHECK
constraints pin the invariants the store relies on: fencing tokens are
positive, owners are non-empty, and a lease never expires before it
was acquired.  The
RLS surface matches 043 exactly (ENABLE + FORCE, ``tenant_isolation``
policy comparing ``tenant_id`` against ``app.tenant_id``, empty setting
yields NULL and matches nothing).

Revision ID: 044
Revises: 043
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "044"
down_revision: str | Sequence[str] | None = "043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONDITION = "\"tenant_id\"::text = NULLIF(current_setting('app.tenant_id', true), '')"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS run_leases (
            tenant_id     TEXT NOT NULL,
            run_id        TEXT NOT NULL,
            owner         TEXT NOT NULL,
            fencing_token BIGINT NOT NULL,
            acquired_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            renewed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at    TIMESTAMPTZ NOT NULL,
            released      BOOLEAN NOT NULL DEFAULT false,
            PRIMARY KEY (tenant_id, run_id),
            CONSTRAINT run_leases_fencing_token_positive CHECK (fencing_token > 0),
            CONSTRAINT run_leases_owner_nonempty CHECK (btrim(owner) <> ''),
            CONSTRAINT run_leases_expires_not_before_acquired CHECK (expires_at >= acquired_at)
        )
        """
    )
    op.execute("ALTER TABLE run_leases ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE run_leases FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON run_leases")
    op.execute(
        f"CREATE POLICY tenant_isolation ON run_leases USING ({_CONDITION}) WITH CHECK ({_CONDITION})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON run_leases")
    op.execute("ALTER TABLE run_leases NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE run_leases DISABLE ROW LEVEL SECURITY")
    op.execute("DROP TABLE IF EXISTS run_leases")
