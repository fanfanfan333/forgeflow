"""038 — durable per-run event log for cross-instance / cross-restart replay.

Additive, reversible. Backs ``forgeflow.runtime.event_log`` so a run's streamed
events survive a restart and are visible to any instance serving the same run.
Append-only, idempotent on the ``(run_id, sequence)`` primary key; ``created_at``
is indexed for the retention sweep (``delete_before``). ``tenant_id`` is nullable
and indexed for the tenant-scoped conventions used elsewhere (it never changes
the primary key).
"""
from alembic import op

revision = "038"
down_revision = "037"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE TABLE run_events ("
        "run_id TEXT NOT NULL, "
        "sequence INTEGER NOT NULL, "
        "event_type TEXT NOT NULL, "
        "payload JSONB NOT NULL DEFAULT '{}'::jsonb, "
        "tenant_id TEXT NULL, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
        "PRIMARY KEY (run_id, sequence))"
    )
    op.execute("CREATE INDEX ix_run_events_created_at ON run_events (created_at)")
    op.execute("CREATE INDEX ix_run_events_tenant_id ON run_events (tenant_id)")


def downgrade():
    op.execute("DROP TABLE run_events")
