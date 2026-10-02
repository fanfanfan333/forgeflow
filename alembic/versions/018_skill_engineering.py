"""018 - INC43 S3 skill engineering runs (additive JSONB archive table).

Adds **one** new table, ``skill_engineering_runs``, that archives the contract /
critique / test / evaluation / revision record of a Skill Engineering loop run
(INC43 §2.3 item 11). It is purely additive: no existing table, column or enum
is touched, and the ``SKILL_STATUSES`` / ``CANDIDATE_STATUSES`` value sets are
unchanged (the six-state lifecycle is a *derived read-only view*, never a stored
enum — INC43 §3.3).

Why a single table with a JSONB ``archive`` column (rather than one table per
loop node): the loop's outputs are already pure, ``to_dict``-able dataclasses
(``forgeflow/skills/contracts.py``), so the archive is a straight JSONB dump —
the shape is the contract, not a schema. Per-node columns are denormalised
**only** where a query is actually needed (``tenant_id`` / ``lifecycle`` /
``passed`` / ``rounds``), which keeps the table honest and indexable without
splitting the record.

Design note (INC43 S3 numbering): the design document names this file
``013_skill_engineering.py``, but revision ``013`` has long been occupied by
``013_tenant_id_text.py`` and the migration head is ``017``. The revision is
therefore, correctly, ``018`` (down_revision ``017``). The *intent* — one new
additive table — is unchanged.

Re-entrancy: every statement is guarded with ``IF NOT EXISTS`` (the extension,
the table and the index), so ``upgrade head`` is idempotent on a fresh and a
populated database alike and running it twice (a delivery requirement) is a
no-op the second time. Importing this module opens no connection — it only
declares the upgrade/downgrade bodies.

Revision ID: 018
Revises: 017
Create Date: 2026-10-06
"""

from alembic import op

revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``gen_random_uuid()`` (the default for ``id``) lives in pgcrypto on older
    # servers; migration 010 already created it, and this is a harmless no-op
    # when it is present (or when the function is in core, PG 13+).
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_engineering_runs (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            -- ``tenant_id`` is opaque TEXT (single-yardstick contract, migration
            -- 013) — a non-UUID tenant slug is a first-class tenant, never
            -- coerced/collapsed into a shared NULL bucket.
            tenant_id       TEXT NOT NULL,
            candidate_id    TEXT,
            lifecycle       TEXT NOT NULL DEFAULT 'DRAFT',
            passed          BOOLEAN NOT NULL DEFAULT FALSE,
            rounds          INTEGER NOT NULL DEFAULT 0,
            degraded_reason TEXT NOT NULL DEFAULT '',
            -- The loop's contract-layer outputs, verbatim (JSONB). ``archive``
            -- is the full ``SkillEngineeringResult.to_dict()``; the three
            -- narrower columns make the common fields queryable without a
            -- JSONB extraction on every read.
            contract        JSONB NOT NULL DEFAULT '{}',
            critique        JSONB NOT NULL DEFAULT '{}',
            evaluation      JSONB NOT NULL DEFAULT '{}',
            archive         JSONB NOT NULL DEFAULT '{}',
            created_by      VARCHAR(128),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_engineering_runs_tenant_created "
        "ON skill_engineering_runs (tenant_id, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS skill_engineering_runs")
