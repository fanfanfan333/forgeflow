"""Give lifecycle events a real insertion order (BIZ-08).

``skill_lifecycle_events`` is append-only, but its order was ``created_at, id``
and ``id`` is a random UUIDv4. Two transitions written in the same clock tick —
a candidate that is published and then deprecated immediately — therefore had
no total order, and ``latest_event`` could return the older state. The state
machine then rejected a legal ``candidate → published → deprecated`` sequence
as illegal.

``seq BIGSERIAL`` is the insertion order the table always implied: it backfills
existing rows in physical order and assigns a strictly increasing value to every
later append. Reads order by ``(created_at, seq)``.

Additive only (new column + index); existing columns and semantics unchanged.
``IF NOT EXISTS`` keeps both upgrade and downgrade idempotent.
"""

from alembic import op

revision = "041"
down_revision = "040"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE skill_lifecycle_events ADD COLUMN IF NOT EXISTS seq BIGSERIAL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_skill_lifecycle_events_tenant_skill_order "
        "ON skill_lifecycle_events (tenant_id, skill_id, created_at, seq)"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_skill_lifecycle_events_tenant_skill_order")
    op.execute("ALTER TABLE skill_lifecycle_events DROP COLUMN IF EXISTS seq")
