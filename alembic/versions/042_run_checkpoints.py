"""042 - Phase 7: ``run_checkpoints`` (run-internal step-boundary resume).

One table, append-only (no UPDATEs, no unique constraint on
(run_id, round): replan attempts re-run round numbers, and history
is the point — latest-wins is by ``seq``, never by timestamp
(the 041 lesson: timestamp ties picked winners by random UUID).

A checkpoint stores refs + minimal state ONLY (intent,
requirements snapshot, per-round acceptance, next step,
codeplane/diff refs, breaker + stuck summaries, approval state +
payload hash, pending writes, trajectory cursor) — never full
message history, tool output text, or codeplane bodies. Those live
in the run records / trajectory store; the checkpoint points at
them. Full per-step snapshots are a proven PG-killer.

Single-writer is enforced at the store layer (PG advisory lock per
run_id), not by this schema; resumable statuses
(interrupted / awaiting_approval / failed) are enforced at the
resume gate, not by a CHECK on run status (the checkpoints table
does not own the run row).
"""

from alembic import op

revision = "042"
down_revision = "041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS run_checkpoints (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       TEXT NOT NULL,
            run_id          TEXT NOT NULL,
            round           INTEGER NOT NULL,
            kind            TEXT NOT NULL CHECK (kind IN ('round', 'approval')),
            schema_version  INTEGER NOT NULL DEFAULT 1,
            state           JSONB NOT NULL DEFAULT '{}'::jsonb,
            seq             BIGINT GENERATED ALWAYS AS IDENTITY,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_run_checkpoints_tenant_run_seq "
        "ON run_checkpoints (tenant_id, run_id, seq DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_run_checkpoints_tenant_run_seq")
    op.execute("DROP TABLE IF EXISTS run_checkpoints")
