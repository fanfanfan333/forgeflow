"""036 — give legacy UUID-workspace tables the same opaque tenant key as the hub.

NULL historical ownership is quarantined. New writes take their tenant from
the transaction, never an untrusted payload or a universal default.
"""
from alembic import op

revision = "036"
down_revision = "035"
branch_labels = None
depends_on = None
TABLES = ("workflow_runs", "approval_requests", "leads", "proposals", "run_metrics", "memory_vectors", "audit_log")


def upgrade():
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ADD COLUMN tenant_id TEXT DEFAULT NULLIF(current_setting('app.tenant_id', true), '')")
        op.execute(f"UPDATE {table} SET tenant_id=workspace_id::text WHERE workspace_id IS NOT NULL")
        op.execute(f"CREATE INDEX idx_{table}_tenant_scope ON {table}(tenant_id)")
    # Policies exist on the parent audit table and its partitions.
    for table in (*TABLES, "audit_log_2025", "audit_log_2026"):
        condition = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '') AND (workspace_id IS NULL OR workspace_id::text = current_setting('app.tenant_id', true))"
        op.execute(f"ALTER POLICY tenant_isolation ON {table} USING ({condition}) WITH CHECK ({condition})")


def downgrade():
    for table in (*TABLES, "audit_log_2025", "audit_log_2026"):
        condition = "workspace_id::text = NULLIF(current_setting('app.tenant_id', true), '')"
        op.execute(f"ALTER POLICY tenant_isolation ON {table} USING ({condition}) WITH CHECK ({condition})")
    for table in TABLES:
        op.execute(f"DROP INDEX idx_{table}_tenant_scope")
        op.execute(f"ALTER TABLE {table} DROP COLUMN tenant_id")
