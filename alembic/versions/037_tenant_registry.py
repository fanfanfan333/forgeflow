"""037 — non-content tenant directory for explicitly scoped background jobs."""
from alembic import op
import sqlalchemy as sa

revision = "037"
down_revision = "036"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE TABLE tenant_registry(tenant_id TEXT PRIMARY KEY)")
    rows = op.get_bind().execute(sa.text("SELECT table_name FROM information_schema.columns WHERE table_schema=current_schema() AND column_name='tenant_id' AND table_name!='tenant_registry'"))
    for (table,) in rows:
        table = table.replace('"', '""')
        op.execute(f'INSERT INTO tenant_registry SELECT DISTINCT tenant_id FROM "{table}" WHERE tenant_id IS NOT NULL ON CONFLICT DO NOTHING')


def downgrade():
    op.execute("DROP TABLE tenant_registry")
