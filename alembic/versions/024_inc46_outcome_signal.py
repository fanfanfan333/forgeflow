"""024 - INC46 T16: ``run_outcomes`` + ``feedback_events`` (the outcome signal).

Before T16 "success" meant little more than "the run finished". This migration
makes success an **auditable multi-signal label** and gives the learning loop a
truthful quality signal:

  * ``run_outcomes``    — one current label per run (``outcome_label`` ∈
                          ACCEPTED_EXPLICIT / ACCEPTED_IMPLICIT / REJECTED /
                          REVISED / REVERTED / ABANDONED / FAILED_SYSTEM /
                          UNKNOWN), plus ``hard_pass`` from the T23 validation
                          stack.
  * ``feedback_events`` — the append-only stream of user signals that produced
                          (or changed) that label, de-duplicated by an
                          idempotency key.

Ownership: migration ``019`` = T01, ``020`` = T02, ``021`` = T05, ``022`` = T15,
``023`` = T10, this ``024`` = T16 — per the §九 migration registry (T16=024,
T21=025, T32=026, T17=027), so no two tasks contend for one migration file and
the chain stays strictly serial: 023 → 024 → 025 → 026.

Honesty discipline (INC46 §8 红线 4 — unmeasured ⇒ NULL, never 0/'')
--------------------------------------------------------------------
* ``hard_pass`` is ``NULL`` when the T23 stack never ran (or did not reach a
  verdict). A NULL here means **not measured**; it must never be coerced to
  ``False`` ("failed") or ``True`` ("passed").
* ``reason`` / ``actor`` are ``NULL`` when nobody supplied them — an absent
  rationale is not an empty rationale.
* ``UNKNOWN`` is a real label, not a missing row: a run with no signal at all is
  still ``UNKNOWN`` and is excluded from **both** the numerator and the
  denominator of any success rate (a run set that is entirely UNKNOWN yields a
  success rate of ``None``, not ``0``).

Additive discipline (§九)
-------------------------
* Two **new** tables only; no existing table or column changes. In particular
  this is deliberately **not** merged with ``skill_feedback`` (T10/023), which
  carries feedback about a *Skill*, whereas ``feedback_events`` signals a single
  *run* — the task book forbids conflating them.

Tenant discipline: ``tenant_id`` is opaque ``TEXT NOT NULL`` (the migration-020
single-yardstick contract); the application layer fails closed, so an
unresolved tenant never writes and never reads. Both tables therefore carry
``tenant_id`` and every index leads with it.

Re-entrancy: ``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS`` —
``upgrade head`` twice is a no-op the second time. Importing this module opens
no connection.

Revision ID: 024
Revises: 023
Create Date: 2026-10-04
"""

from alembic import op

revision = "024"
down_revision = "023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``gen_random_uuid()`` (the ``id`` default) lives in pgcrypto on older
    # servers; migration 010 already created it. Harmless no-op when present.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS run_outcomes (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            -- Opaque TEXT tenant (never coerced/collapsed) — fail-closed writes.
            tenant_id       TEXT NOT NULL,
            run_id          TEXT NOT NULL,
            -- ACCEPTED_EXPLICIT | ACCEPTED_IMPLICIT | REJECTED | REVISED |
            -- REVERTED | ABANDONED | FAILED_SYSTEM | UNKNOWN
            outcome_label   VARCHAR(24) NOT NULL DEFAULT 'UNKNOWN',
            -- From the T23 validation stack: TRUE / FALSE / NULL(=未测量，红线 4).
            hard_pass       BOOLEAN,
            -- NULL when nobody gave a reason (未测量 ⇒ NULL，不默认 '').
            reason          TEXT,
            labeled_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            -- One current label per run, per tenant (UPSERT target).
            CONSTRAINT uq_run_outcomes_tenant_run UNIQUE (tenant_id, run_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_run_outcomes_tenant_label "
        "ON run_outcomes (tenant_id, outcome_label, labeled_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_run_outcomes_tenant_run "
        "ON run_outcomes (tenant_id, run_id)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback_events (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       TEXT NOT NULL,
            run_id          TEXT NOT NULL,
            -- approval | like | reject | revise | revert | abandon | system
            kind            VARCHAR(24) NOT NULL,
            -- NULL when the actor is unknown (未测量 ⇒ NULL).
            actor           TEXT,
            -- Caller-supplied idempotency key: re-submitting the same key must
            -- never double-count (T16 阴性探针).
            idempotency_key TEXT NOT NULL,
            -- NULL when no note was supplied (未测量 ⇒ NULL，不默认 '').
            note            TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            -- Idempotency is scoped per tenant: the same key under a different
            -- tenant is a different event and must not collide.
            CONSTRAINT uq_feedback_events_tenant_key UNIQUE (tenant_id, idempotency_key)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_feedback_events_tenant_run "
        "ON feedback_events (tenant_id, run_id, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_feedback_events_tenant_run")
    op.execute("DROP TABLE IF EXISTS feedback_events")
    op.execute("DROP INDEX IF EXISTS idx_run_outcomes_tenant_run")
    op.execute("DROP INDEX IF EXISTS idx_run_outcomes_tenant_label")
    op.execute("DROP TABLE IF EXISTS run_outcomes")
