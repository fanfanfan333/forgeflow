"""012 - INC8 Agent Evaluation System — judge-sample table + ``skill_refs`` column.

Implements docs/sop/11-INC8-AGENT-EVALUATION-DESIGN.md §3.2. New table:

  ``agent_eval_samples`` — judge (groundedness / hallucination) plus
  deterministic-proxy samples that **no existing table can hold**.

Plus one additive column:

  ``context_build_stats.skill_refs TEXT[]`` — per-run skill reuse (metric #9, PG)

Why a NEW table and not ``run_metrics`` (design §12.2): ``run_metrics.run_id`` is
a UUID with ``REFERENCES workflow_runs(id)`` (migration 003), but hub runs never
land in ``workflow_runs`` → writing judge scores there is a foreign-key dead end.
``run_id`` here is deliberately ``TEXT`` with **no FK**.

Additive-only / re-entrant: every statement uses ``IF NOT EXISTS``; revision 011
and everything before it are untouched. Importing this module opens no
connection — it only declares the upgrade/downgrade bodies.

Revision ID: 012
Revises: 011
Create Date: 2026-09-24
"""

from alembic import op

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_eval_samples (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id    UUID,
            run_id       TEXT,
            metric_name  VARCHAR(64)  NOT NULL,
            metric_value DOUBLE PRECISION NOT NULL,
            metric_unit  VARCHAR(32),
            dimension    VARCHAR(16)  NOT NULL,
            source       VARCHAR(32)  NOT NULL DEFAULT 'judge',
            dataset      VARCHAR(128),
            meta         JSONB NOT NULL DEFAULT '{}',
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_eval_samples_tenant_time "
        "ON agent_eval_samples (tenant_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_eval_samples_run "
        "ON agent_eval_samples (run_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_eval_samples_metric "
        "ON agent_eval_samples (metric_name, created_at DESC)"
    )
    op.execute(
        "ALTER TABLE context_build_stats "
        "ADD COLUMN IF NOT EXISTS skill_refs TEXT[] NOT NULL DEFAULT '{}'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE context_build_stats DROP COLUMN IF EXISTS skill_refs")
    op.execute("DROP TABLE IF EXISTS agent_eval_samples")
