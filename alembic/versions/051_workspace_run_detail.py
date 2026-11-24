"""Persist a bounded run-detail projection so a restart keeps the run body (BIZ-14).

``workspace_runs`` used to store only the header + artifacts, so a run hydrated
after a restart came back with an empty step list and ``detail_retained=False``.
The column is additive and default-safe: every pre-051 row reads as ``{}``, which
the read path treats as "no detail recorded" instead of "the run did nothing".

(Ported from the snapshot branch's 042, renumbered to 051 for main's chain.)
"""

from alembic import op

revision = "051"
down_revision = "050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE workspace_runs ADD COLUMN IF NOT EXISTS detail JSONB NOT NULL DEFAULT '{}'::jsonb"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE workspace_runs DROP COLUMN IF EXISTS detail")
