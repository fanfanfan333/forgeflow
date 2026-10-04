"""025 - INC46 T21: ``pending_actions`` (the HITL pause-for-human primitive).

One table backs every "the run must stop and wait for a person" moment — the
shared mechanism for clarification, diff approval and permission confirmation:

  * ``kind``   ∈ clarify | approve_diff | confirm_permission
  * ``status`` ∈ waiting | resolved | expired | cancelled
  * ``expires_at`` — the deadline (default 24h); past it the action is ``expired``
    and can never be an implicit approval (超时 fail-closed, 红线 21).

Ownership: migration ``019`` = T01, ``020`` = T02, ``021`` = T05, ``022`` = T15,
``023`` = T10, ``024`` = T16, this ``025`` = T21 — per the §九 migration registry
(T16=024, T21=025, T32=026, T17=027), so the chain stays strictly serial:
023 → 024 → 025 → 026.

Additive discipline (§九)
-------------------------
* One **new** table only; no existing table or column changes, and in particular
  the run-status vocabulary in ``forgeflow.workspace.store`` is left untouched
  (the additive ``expired`` run status lives in ``forgeflow.hitl.pending``).

Honesty discipline (INC46 §8 红线 4 — unmeasured ⇒ NULL, never 0/'')
--------------------------------------------------------------------
* ``payload``       is ``NULL`` when nothing human-facing was supplied — an
  absent question is not an empty one.
* ``resolution`` / ``resolved_at`` / ``resolved_by`` are ``NULL`` until a human
  acts; nothing is coerced to ``''`` / ``0``.
* ``expires_at`` is ``NULL`` when no deadline is configured (nullable on purpose).

Tenant discipline: ``tenant_id`` is opaque ``TEXT NOT NULL`` (the migration-020
single-yardstick contract); the application layer fails closed, so an unresolved
tenant never writes and never reads (its reads return the empty set). Every index
leads with ``tenant_id``.

Re-entrancy: ``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS`` —
``upgrade head`` twice is a no-op the second time. Importing this module opens no
connection.

Revision ID: 025
Revises: 024
Create Date: 2026-10-04
"""

from alembic import op

revision = "025"
down_revision = "024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``gen_random_uuid()`` (the ``id`` default) lives in pgcrypto on older
    # servers; migration 010 already created it. Harmless no-op when present.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_actions (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            -- Opaque TEXT tenant (never coerced/collapsed) — fail-closed writes.
            tenant_id       TEXT NOT NULL,
            -- Opaque per-tenant action id (the public ``pending_id``).
            pending_id      TEXT NOT NULL,
            run_id          TEXT NOT NULL,
            -- clarify | approve_diff | confirm_permission
            kind            VARCHAR(24) NOT NULL,
            -- Human-facing context. NULL when nothing was supplied (未测量 ⇒ NULL).
            payload         JSONB,
            -- waiting | resolved | expired | cancelled
            status          VARCHAR(16) NOT NULL DEFAULT 'waiting',
            -- The human's decision. NULL until resolved.
            resolution      TEXT,
            -- The deadline (default 24h). NULL = no deadline configured.
            expires_at      TIMESTAMPTZ,
            resolved_at     TIMESTAMPTZ,
            -- NULL when the actor is unknown (未测量 ⇒ NULL).
            resolved_by     TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            -- One action per (tenant, pending_id) — the UPSERT target.
            CONSTRAINT uq_pending_actions_tenant_pending UNIQUE (tenant_id, pending_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_actions_tenant_status "
        "ON pending_actions (tenant_id, status, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_actions_tenant_run "
        "ON pending_actions (tenant_id, run_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_pending_actions_tenant_run")
    op.execute("DROP INDEX IF EXISTS idx_pending_actions_tenant_status")
    op.execute("DROP TABLE IF EXISTS pending_actions")
