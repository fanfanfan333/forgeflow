"""Persist an immutable digest for idempotent workspace requests."""
from alembic import op

revision = "040"
down_revision = "039"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE workspace_runs ADD COLUMN request_digest VARCHAR(64)")


def downgrade():
    op.execute("ALTER TABLE workspace_runs DROP COLUMN request_digest")
