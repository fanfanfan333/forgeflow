"""011 - INC2 Cost / SLO / Evolution tables + column extensions.

Implements the incremental persistence layer of docs/sop/05-ARCHITECTURE-INC2.md
§3. New tables:

  cost_budgets, skill_ratings, context_build_stats

Plus additive columns:
  skill_listings(shared, listed_by, rating, rating_count)
  experiences(conflict_with, merged_from, dedup_key, confidence)
  skills(last_used_at, retire_suggested)
  agent_approvals(kind)

Every statement uses ``IF NOT EXISTS`` / ``ADD COLUMN IF NOT EXISTS`` so the
migration is re-entrant and safe to re-run. Importing this module never opens a
connection — it only declares the upgrade/downgrade bodies (alembic runs them).

The downgrade is a strict mirror of the upgrade: it drops exactly the columns
and tables added here (``DROP ... IF EXISTS`` makes it symmetric and idempotent),
leaving revision 010 and everything before it untouched.

Revision ID: 011
Revises: 010
Create Date: 2026-09-23
"""

from alembic import op

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- A1 cost_budgets (§2.1) -----------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS cost_budgets (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id    UUID,
            scope        VARCHAR(16) NOT NULL DEFAULT 'tenant',
            scope_id     VARCHAR(128),
            limit_amount NUMERIC(14,4) NOT NULL DEFAULT 0,
            warn_ratio   NUMERIC(4,3)  NOT NULL DEFAULT 0.800,
            currency     VARCHAR(8)    NOT NULL DEFAULT 'CNY',
            on_exceed    JSONB NOT NULL DEFAULT '["swap_model","trim_context","pause_noncritical"]',
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, scope, scope_id)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_cost_budgets_tenant ON cost_budgets (tenant_id)")

    # --- A4 skill_ratings (§2.4) ----------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_ratings (
            id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            listing_id UUID NOT NULL,
            tenant_id  UUID,
            user_id    VARCHAR(128),
            score      SMALLINT NOT NULL,
            comment    TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_skill_ratings_listing ON skill_ratings (listing_id)")

    # --- A5 context_build_stats (§3) ------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS context_build_stats (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id         UUID,
            run_id            UUID,
            tokens_raw        INTEGER NOT NULL DEFAULT 0,
            tokens_used       INTEGER NOT NULL DEFAULT 0,
            compression_ratio DOUBLE PRECISION NOT NULL DEFAULT 1,
            hit_rate          DOUBLE PRECISION NOT NULL DEFAULT 0,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_context_build_stats_tenant "
        "ON context_build_stats (tenant_id)"
    )

    # --- A4 skill_listings additive columns (§2.4) ----------------------------
    op.execute(
        "ALTER TABLE skill_listings ADD COLUMN IF NOT EXISTS shared BOOLEAN NOT NULL DEFAULT FALSE"
    )
    op.execute(
        "ALTER TABLE skill_listings ADD COLUMN IF NOT EXISTS listed_by VARCHAR(128)"
    )
    op.execute(
        "ALTER TABLE skill_listings ADD COLUMN IF NOT EXISTS rating NUMERIC(3,2) NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE skill_listings ADD COLUMN IF NOT EXISTS rating_count INTEGER NOT NULL DEFAULT 0"
    )

    # --- B4 experiences additive columns (§2.11) ------------------------------
    op.execute(
        "ALTER TABLE experiences ADD COLUMN IF NOT EXISTS conflict_with UUID[] NOT NULL DEFAULT '{}'"
    )
    op.execute(
        "ALTER TABLE experiences ADD COLUMN IF NOT EXISTS merged_from UUID[] NOT NULL DEFAULT '{}'"
    )
    op.execute(
        "ALTER TABLE experiences ADD COLUMN IF NOT EXISTS dedup_key TEXT"
    )
    op.execute(
        "ALTER TABLE experiences ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION"
    )

    # --- A3 skills additive columns (§2.3) ------------------------------------
    op.execute(
        "ALTER TABLE skills ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ"
    )
    op.execute(
        "ALTER TABLE skills ADD COLUMN IF NOT EXISTS retire_suggested BOOLEAN NOT NULL DEFAULT FALSE"
    )

    # --- Evolution approval kind (§7.5) ---------------------------------------
    op.execute(
        "ALTER TABLE agent_approvals ADD COLUMN IF NOT EXISTS kind VARCHAR(32)"
    )


def downgrade() -> None:
    # Reverse the additive columns first (drops are safe/no-ops when absent).
    op.execute("ALTER TABLE agent_approvals DROP COLUMN IF EXISTS kind")

    op.execute("ALTER TABLE skills DROP COLUMN IF EXISTS retire_suggested")
    op.execute("ALTER TABLE skills DROP COLUMN IF EXISTS last_used_at")

    op.execute("ALTER TABLE experiences DROP COLUMN IF EXISTS confidence")
    op.execute("ALTER TABLE experiences DROP COLUMN IF EXISTS dedup_key")
    op.execute("ALTER TABLE experiences DROP COLUMN IF EXISTS merged_from")
    op.execute("ALTER TABLE experiences DROP COLUMN IF EXISTS conflict_with")

    op.execute("ALTER TABLE skill_listings DROP COLUMN IF EXISTS rating_count")
    op.execute("ALTER TABLE skill_listings DROP COLUMN IF EXISTS rating")
    op.execute("ALTER TABLE skill_listings DROP COLUMN IF EXISTS listed_by")
    op.execute("ALTER TABLE skill_listings DROP COLUMN IF EXISTS shared")

    # Drop the new tables (no inter-table FK dependencies, order is cosmetic).
    for table in (
        "context_build_stats",
        "skill_ratings",
        "cost_budgets",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table}")
