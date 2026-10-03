"""021 - INC46 T05: ``skill_versions.created_from_runs`` (evolution provenance).

Adds one **nullable, non-backfilled** column that records which execution runs an
auto-evolved Skill version was derived from — the "Skill 不是静态 Prompt，而是可
持续进化的执行资产" provenance link (INC46 §1.5 step ⑤ / §7-T05 判据 3).

Ownership: migration ``019`` = T01 (run_steps), ``020`` = T02
(skill_rule_suggestions), this ``021`` = T05 — per delivery ruling **D-R1**, so
no two tasks contend for one migration file.

Honesty discipline
------------------
* The column is **NULL** for every pre-existing version: no row is backfilled,
  because provenance we did not observe cannot be fabricated. "No provenance"
  and "provenance = empty list" stay distinguishable.
* The write in ``skills.evolution_loop._persist_provenance`` is **additive and
  best-effort** — a failure to record provenance never blocks or fakes a
  version. The in-process generation ledger stays authoritative for the
  ``MAX_EVOLVE_GENERATIONS`` cap.

Re-entrancy: ``ADD COLUMN IF NOT EXISTS`` — ``upgrade head`` twice is a no-op
the second time. Importing this module opens no connection.

Revision ID: 021
Revises: 020
Create Date: 2026-10-03
"""

from alembic import op

revision = "021"
down_revision = "020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE skill_versions
        ADD COLUMN IF NOT EXISTS created_from_runs JSONB
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE skill_versions
        DROP COLUMN IF EXISTS created_from_runs
        """
    )
