"""043 — RLS policy for ``run_checkpoints`` (created by 042 without one).

Migration 042 created ``run_checkpoints`` with no row-level
security, so the table sat outside the tenant-isolation scheme that
035 applied to every other tenant-scoped table and 039 applied to
``feedback_run_owners`` / ``run_events``. The checkpoint store always
sets ``app.tenant_id`` on its connection (see
``PostgresCheckpointStore._connect``), so a forced tenant-isolation
policy is compatible with it — and it closes the hole where a
connection that forgot the tenant filter could read or write another
tenant's resume cursors (which carry approval state and pending
writes).

Modeled on 039's explicit per-table form (and 035's fail-closed
condition): an empty tenant setting yields NULL, which matches no
rows — never "no restriction".
"""

from alembic import op

revision = "043"
down_revision = "042"
branch_labels = None
depends_on = None

_CONDITION = "\"tenant_id\"::text = NULLIF(current_setting('app.tenant_id', true), '')"


def upgrade() -> None:
    op.execute("ALTER TABLE run_checkpoints ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE run_checkpoints FORCE ROW LEVEL SECURITY")
    # DROP first so a re-run converges instead of failing on the
    # already-existing policy (CREATE POLICY has no IF NOT EXISTS).
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON run_checkpoints")
    op.execute(
        f"CREATE POLICY tenant_isolation ON run_checkpoints "
        f"USING ({_CONDITION}) WITH CHECK ({_CONDITION})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON run_checkpoints")
    op.execute("ALTER TABLE run_checkpoints NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE run_checkpoints DISABLE ROW LEVEL SECURITY")
