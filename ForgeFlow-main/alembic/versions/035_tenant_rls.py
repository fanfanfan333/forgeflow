"""035 — database-enforced tenant boundaries and durable refresh scope.

Identity/membership tables are global authentication metadata, not tenant
content. Legacy NULL content remains quarantined (no guessed ownership).
"""
from alembic import op
import sqlalchemy as sa

revision = "035"
down_revision = "034"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE auth_refresh_tokens ADD COLUMN workspace_id TEXT")
    op.execute("ALTER TABLE approval_requests ADD COLUMN decision_claimed_at TIMESTAMPTZ")
    op.execute("""CREATE TABLE tenant_memberships (
        tenant_id TEXT NOT NULL, user_id UUID NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
        PRIMARY KEY (tenant_id,user_id))""")
    # Existing identity assignments are the only authority for membership.
    op.execute("""INSERT INTO tenant_memberships(tenant_id,user_id)
        SELECT COALESCE(workspace_id::text, 'default'), id FROM auth_users
        WHERE NOT disabled ON CONFLICT DO NOTHING""")
    op.execute("""INSERT INTO tenant_memberships(tenant_id,user_id)
        SELECT workspace_id::text,user_id FROM workspace_members ON CONFLICT DO NOTHING""")
    rows = op.get_bind().execute(sa.text("""
        SELECT table_name,column_name FROM information_schema.columns
        WHERE table_schema=current_schema() AND column_name IN ('tenant_id','workspace_id')
        AND table_name NOT IN ('auth_users','auth_refresh_tokens','workspace_members','tenant_memberships')
        ORDER BY table_name
    """))
    for table, column in rows:
        # Names come from the catalogue; quote identifiers even here.
        table, column = table.replace('"', '""'), column.replace('"', '""')
        condition = f'"{column}"::text = NULLIF(current_setting(\'app.tenant_id\', true), \'\')'
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(f'CREATE POLICY tenant_isolation ON "{table}" USING ({condition}) WITH CHECK ({condition})')


def downgrade():
    rows = op.get_bind().execute(sa.text("SELECT tablename FROM pg_policies WHERE schemaname=current_schema() AND policyname='tenant_isolation'"))
    for (table,) in rows:
        table = table.replace('"', '""')
        op.execute(f'DROP POLICY tenant_isolation ON "{table}"')
        op.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
    op.execute("DROP TABLE tenant_memberships")
    op.execute("ALTER TABLE approval_requests DROP COLUMN decision_claimed_at")
    op.execute("ALTER TABLE auth_refresh_tokens DROP COLUMN workspace_id")
