"""019 - INC46 T01: additive materialisation of the real execution trace.

The defect this migration unblocks
----------------------------------
``run_steps`` (migration 010) declared ``latency_ms`` as
``DOUBLE PRECISION NOT NULL DEFAULT 0``. INC46 T01 materialises the **real**
execution trace into ``run_steps`` — one row per tool invocation, carrying the
handler's honest ``latency_ms``. A handler that was never measured (a
``blocked`` / ``unavailable`` / ``refused`` step) must be stored as ``NULL``,
**never** a fabricated ``0`` (data-honesty red line). ``NOT NULL DEFAULT 0``
made that impossible: any ``NULL`` write failed, and an omitted value silently
became a fake ``0``. This is the one place INC46 cannot be zero-migration.

What this migration does (all additive / re-entrant)
----------------------------------------------------
  * ``run_steps.latency_ms``  → ``DROP NOT NULL`` + ``DROP DEFAULT`` so an
    unmeasured latency is stored as ``NULL``;
  * five **nullable** top-level columns so the trace is queryable without a
    JSONB extraction on every read — ``status`` / ``artifact_ref`` /
    ``verification`` / ``actor_user_id`` / ``attempt``;
  * one index on ``(tenant_id, created_at DESC)`` for tenant-scoped reads.

There is **no data backfill**: every new column is nullable, so existing rows
(and every existing consumer) are untouched. ``run_steps`` has **zero**
application-layer readers (only migrations + docs reference it), so widening its
surface cannot break a run — see the INC46 design §0-F2 / §9-1.

Migration granularity (delivery ruling D-R1)
--------------------------------------------
The INC46 design §5/#1 assumed a *single* ``019`` carrying both the ``run_steps``
change **and** the T02 rule table. Delivery split that into one migration per
owning task. This migration is **T01 only** (``run_steps``); the rule table
(``skill_rule_suggestions``) is migration ``020`` (T02) and
``skill_versions.created_from_runs`` is ``021`` (T05). The revision id is kept
as ``019`` (the design's number) and ``down_revision`` points at the live head
``018``.

Re-entrancy
-----------
Every statement is ``ALTER ... DROP NOT NULL`` / ``ALTER ... DROP DEFAULT`` /
``ADD COLUMN IF NOT EXISTS`` / ``CREATE INDEX IF NOT EXISTS`` — PostgreSQL
no-ops a repeat (dropping an absent NOT NULL / DEFAULT does not raise), so
``upgrade head`` twice is a no-op the second time, on an empty and a populated
database alike.

Downgrade honesty
-----------------
The downgrade drops only the columns/index INC46 added. It deliberately does
**not** re-add ``latency_ms NOT NULL`` / ``DEFAULT 0`` — doing so would either
fail on the ``NULL`` rows T01 legitimately stored, or coerce them to a fake
``0`` (falsifying an unmeasured latency). ``010``'s own downgrade drops the
whole table, so there is no residual risk.

Revision ID: 019
Revises: 018
Create Date: 2026-10-03
"""

from alembic import op

revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- R1: latency_ms becomes "measured or NULL" (never a fabricated 0) -----
    # ``DROP NOT NULL`` on an already-nullable column is a no-op; likewise
    # ``DROP DEFAULT`` when no default is set. Both keep the migration re-entrant.
    op.execute("ALTER TABLE run_steps ALTER COLUMN latency_ms DROP NOT NULL")
    op.execute("ALTER TABLE run_steps ALTER COLUMN latency_ms DROP DEFAULT")

    # --- R2: queryable, all-nullable trace columns (no backfill) --------------
    op.execute("ALTER TABLE run_steps ADD COLUMN IF NOT EXISTS status VARCHAR(24)")
    op.execute("ALTER TABLE run_steps ADD COLUMN IF NOT EXISTS artifact_ref TEXT")
    op.execute("ALTER TABLE run_steps ADD COLUMN IF NOT EXISTS verification JSONB")
    op.execute("ALTER TABLE run_steps ADD COLUMN IF NOT EXISTS actor_user_id VARCHAR(128)")
    op.execute("ALTER TABLE run_steps ADD COLUMN IF NOT EXISTS attempt INTEGER")

    # --- tenant-scoped reads (T02 pattern mining / T06 insights) --------------
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_run_steps_tenant "
        "ON run_steps (tenant_id, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_run_steps_tenant")
    op.execute("ALTER TABLE run_steps DROP COLUMN IF EXISTS attempt")
    op.execute("ALTER TABLE run_steps DROP COLUMN IF EXISTS actor_user_id")
    op.execute("ALTER TABLE run_steps DROP COLUMN IF EXISTS verification")
    op.execute("ALTER TABLE run_steps DROP COLUMN IF EXISTS artifact_ref")
    op.execute("ALTER TABLE run_steps DROP COLUMN IF EXISTS status")
    # NOTE (honesty): ``latency_ms`` is intentionally left nullable with no
    # default. Re-adding ``NOT NULL DEFAULT 0`` would falsify every unmeasured
    # (NULL) latency this migration made storable. 010's downgrade drops the
    # table wholesale, so nothing is left behind.
