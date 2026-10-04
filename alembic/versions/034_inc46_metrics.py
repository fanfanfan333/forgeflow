"""034 — INC46 T36: ``metric_snapshots`` / ``benchmark_runs``。

Why these tables exist
----------------------
T36 要把「机制正确」升级为「**效果更好**」的可证伪证据：

  * ``metric_snapshots`` —— 一次指标聚合的**快照**。``metrics`` 是 ``{name: value|null}``
    的 JSONB；**未测量的指标写成 JSON ``null``，绝不写 0**（红线 4）。R8 门禁比较的
    「上一基线」读的就是该租户最近一张快照。
  * ``benchmark_runs``   —— 一次端到端基准运行的**结果**。``corpus_hash`` 冻结基准集
    指纹；基准集被改动（哈希不符）时 runner **拒绝运行**，因此不会产生行。``report``
    存各类用例的通过矩阵。

纪律（INC46 §8）
----------------
* 红线 5：``tenant_id TEXT NOT NULL`` 是每表第一列；无租户即 fail-closed（应用层拒写）。
* 红线 4：未测量字段允许 ``NULL``，**不得默认 0**（``metrics`` 里的 null；``window_hours`` 可空）。
* 红线 6：快照 / 基准结果只 INSERT，不改写历史。
* 红线 20：改动**加性**（新表 + 新索引），不改既有列语义。
* 幂等：``CREATE TABLE/INDEX IF NOT EXISTS`` ⇒ ``upgrade head`` 连跑两次，第二次 no-op。

Revision ID: 034
Revises: 033
Create Date: 2026-10-04
"""

from alembic import op

revision = "034"
down_revision = "033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``gen_random_uuid()`` lives in pgcrypto on older servers; migration 010
    # already created it. Harmless no-op when present.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # --- metric_snapshots: one metrics aggregate per (tenant, window) --- #
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS metric_snapshots (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            -- Opaque TEXT tenant (never coerced/collapsed) — fail-closed writes.
            tenant_id    TEXT NOT NULL,
            -- {metric_name: value | null}; a JSON null means "unmeasured", never 0.
            metrics      JSONB NOT NULL DEFAULT '{}'::jsonb,
            -- Observation window length; NULL when the caller did not measure it.
            window_hours DOUBLE PRECISION,
            -- Row counts are measured facts (0 is a real count, not a placeholder).
            total_tasks  INTEGER NOT NULL DEFAULT 0,
            labeled_runs INTEGER NOT NULL DEFAULT 0,
            generated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_metric_snapshots_tenant_time "
        "ON metric_snapshots (tenant_id, generated_at DESC)"
    )

    # --- benchmark_runs: one end-to-end benchmark execution per row --- #
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS benchmark_runs (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id   TEXT NOT NULL,
            -- Frozen corpus fingerprint (sha256); a mismatch refuses the run.
            corpus_hash VARCHAR(64) NOT NULL,
            total_cases INTEGER NOT NULL DEFAULT 0,
            passed      INTEGER NOT NULL DEFAULT 0,
            failed      INTEGER NOT NULL DEFAULT 0,
            errors      INTEGER NOT NULL DEFAULT 0,
            skipped     INTEGER NOT NULL DEFAULT 0,
            -- Per-category pass matrix + per-case verdicts.
            report      JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_benchmark_runs_tenant_time "
        "ON benchmark_runs (tenant_id, created_at DESC)"
    )


def downgrade() -> None:
    # Additive-only (§九): dropping the tables is the exact inverse.
    op.execute("DROP TABLE IF EXISTS benchmark_runs")
    op.execute("DROP TABLE IF EXISTS metric_snapshots")
