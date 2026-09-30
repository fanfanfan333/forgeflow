"""016 - INC32 workspace runtime — the ``workspace_runs`` table.

Additive-only, single new table. Implements ``docs/sop/INC32-DESIGN.md``
ADR-02: the platform previously had **no** ``session_id`` / ``parent_run_id``
concept — hub runs live only in the process-lifetime
``forgeflow.runtime.orchestrator.MemoryRunStore`` (a restart drops them). This
table persists the *minimum relationship facts* so the left-hand
"history / session" column and the Follow-up parent chain (AC-39) survive a
restart — the full run *body* (steps / plan / observations / tool invocations)
stays in memory by design (a large persistence overhaul is explicitly out of
scope).

Column choices (mirrors the ADR-02 SQL block verbatim):
  * ``run_id`` is the primary key — it soft-joins to the existing
    ``experiences.run_id`` / ``run_steps.run_id`` with **no** foreign key and
    **no** change to any existing table.
  * ``tenant_id`` is ``TEXT`` by the post-``013`` hub convention (an opaque
    tenant id, never silently dropped to NULL).
  * ``created_at`` / ``completed_at`` / ``updated_at`` are ISO-8601 ``TEXT`` so
    both backends round-trip them byte-for-byte with no timezone drift.
  * ``declared_inputs`` / ``artifacts`` are ``JSONB`` (queried as opaque
    documents). ``artifacts`` is what keeps AC-31 (artifact download) serviceable
    after a restart even though the run body is gone.

Re-entrancy: every statement uses ``IF NOT EXISTS``, so ``upgrade head`` is
idempotent on a fresh and a populated database alike, and running it twice
(INC32 T01 acceptance) is a no-op the second time. Revision ``015`` and earlier
are untouched. Importing this module opens no connection — it only declares the
upgrade/downgrade bodies.

Revision ID: 016
Revises: 015
Create Date: 2026-10-04
"""

from alembic import op

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS workspace_runs (
            run_id          TEXT PRIMARY KEY,
            tenant_id       TEXT NOT NULL,
            session_id      TEXT NOT NULL,
            parent_run_id   TEXT,
            actor_user_id   TEXT NOT NULL DEFAULT '',
            actor_role      TEXT NOT NULL DEFAULT '',
            intent          TEXT NOT NULL DEFAULT '',
            title           TEXT NOT NULL DEFAULT '',
            workflow_type   TEXT NOT NULL DEFAULT 'generic',
            status          VARCHAR(32) NOT NULL DEFAULT 'running',
            outcome         VARCHAR(32) NOT NULL DEFAULT '',
            declared_inputs JSONB NOT NULL DEFAULT '{}',
            artifacts       JSONB NOT NULL DEFAULT '[]',
            created_at      TEXT NOT NULL,
            completed_at    TEXT,
            updated_at      TEXT NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_workspace_runs_tenant_time "
        "ON workspace_runs (tenant_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_workspace_runs_tenant_session "
        "ON workspace_runs (tenant_id, session_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_workspace_runs_parent "
        "ON workspace_runs (parent_run_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS workspace_runs")
