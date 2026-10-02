"""017 - INC42 workspace history lifecycle columns.

Additive-only, three new columns on the INC32 ``workspace_runs`` table plus one
index. Implements ``_inc42_brief.md`` T1:

  * ``deleted_at``   — soft-delete marker (``NULL`` = live, an ISO-8601 string =
    the session was soft-deleted; the row is **kept** so the audit chain
    survives — Q5=B). Every read path filters ``deleted_at IS NULL``.
  * ``runtime_mode`` — the executor that produced the run (``react`` / ``llm`` /
    ``deterministic``), persisted so a historical run stops being mislabelled
    ``deterministic`` after a restart (INC42 defect ④ root cause). ``''`` means
    "not recorded" (every pre-017 row) — the honest unknown, never coerced.
  * ``llm``          — the executor provenance dict (models built / degraded /
    rounds / final_answer), persisted alongside ``runtime_mode`` so
    ``GET /runs/{id}`` can prove a model really ran for a historical run.

Re-entrancy: every statement is guarded with ``IF NOT EXISTS`` (``ADD COLUMN``
and ``CREATE INDEX``), so ``upgrade head`` is idempotent on a fresh and a
populated database alike and running it twice (INC42 T1 acceptance) is a no-op
the second time. Revision ``016`` and earlier are untouched. Importing this
module opens no connection — it only declares the upgrade/downgrade bodies.

Revision ID: 017
Revises: 016
Create Date: 2026-10-05
"""

from alembic import op

revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``deleted_at`` — NULL means "live"; a string (ISO-8601) means soft-deleted.
    op.execute(
        "ALTER TABLE workspace_runs ADD COLUMN IF NOT EXISTS deleted_at TEXT"
    )
    # ``runtime_mode`` / ``llm`` — the executor provenance, so a historical run
    # keeps its real runtime truth. ``''`` / ``'{}'`` are the honest "not
    # recorded" defaults for every pre-017 row (never invented as 'deterministic').
    op.execute(
        "ALTER TABLE workspace_runs "
        "ADD COLUMN IF NOT EXISTS runtime_mode TEXT NOT NULL DEFAULT ''"
    )
    op.execute(
        "ALTER TABLE workspace_runs "
        "ADD COLUMN IF NOT EXISTS llm JSONB NOT NULL DEFAULT '{}'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_workspace_runs_tenant_deleted "
        "ON workspace_runs (tenant_id, deleted_at)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE workspace_runs DROP COLUMN IF EXISTS deleted_at")
    op.execute("ALTER TABLE workspace_runs DROP COLUMN IF EXISTS runtime_mode")
    op.execute("ALTER TABLE workspace_runs DROP COLUMN IF EXISTS llm")
