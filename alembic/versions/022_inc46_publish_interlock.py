"""022 - INC46 T15: ``publish_approvals`` (the publish-interlock decision table).

T05 used to publish a regression-passing candidate automatically. T15 locks
auto-publish behind the R1–R8 capability probes (INC46 §3.3) and makes every
publish decision auditable: each staging / approval / rejection of a skill
version appends one row here.

Ownership: migration ``019`` = T01, ``020`` = T02, ``021`` = T05, this ``022``
= T15 — per delivery ruling **D-R1** and the §九 registry (T15=022, T10=023),
so no two tasks contend for one migration file.

Additive discipline
-------------------
* One **new** table only; no existing column's semantics change. The
  ``pending_approval`` publish-state value is additive at the code level
  (``skills/publish_interlock.PUBLISH_STATES`` / ``EvolutionOutcome.
  publish_state``) — evolution outcomes are not a persisted table, so there is
  no enum column to alter here.
* Unmeasured ⇒ NULL, never 0/'' (INC46 §8): a ``pending`` row has
  ``approver = NULL`` (no approval happened yet); an approval without an
  explicit reason keeps ``reason = NULL``.

Tenant discipline: ``tenant_id`` is opaque ``TEXT NOT NULL`` (the migration-020
single-yardstick contract). The application layer fails closed — an unresolved
tenant never writes and never reads.

Re-entrancy: ``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS`` —
``upgrade head`` twice is a no-op the second time. Importing this module opens
no connection.

Revision ID: 022
Revises: 021
Create Date: 2026-10-04
"""

from alembic import op

revision = "022"
down_revision = "021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``gen_random_uuid()`` (the ``id`` default) lives in pgcrypto on older
    # servers; migration 010 already created it. Harmless no-op when present.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS publish_approvals (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            -- Opaque TEXT tenant (never coerced/collapsed) — fail-closed writes.
            tenant_id   TEXT NOT NULL,
            skill_id    TEXT NOT NULL,
            version     TEXT NOT NULL,
            -- NULL while decision='pending' — 未发生的批准绝不伪造批准人。
            approver    TEXT,
            -- pending | approved | rejected（publish_interlock.DECISIONS）。
            decision    VARCHAR(16) NOT NULL,
            -- NULL when the decider gave no reason (未测量 ⇒ NULL，不默认 '')。
            reason      TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_publish_approvals_tenant "
        "ON publish_approvals (tenant_id, skill_id, version, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS publish_approvals")
