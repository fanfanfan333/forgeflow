"""032 — INC46 T34: ``skill_rollouts`` / ``rollout_metrics`` / ``skill_rollbacks``。

Why these tables exist
----------------------
T09 只在选择层预留了「版本流量解析」扩展点，**不实现灰度分流**；T16 产出了
outcome 标签，T17 产出了 held-out 评测。T34 把这三样接起来：候选版本先以
**5% → 25% → 100%** 逐档放量，每档用**有标签 run** 的区间下界（Wilson）与
incumbent 对照；一旦劣化即**自动回滚**。要让「灰度」与「回滚」**可审计、可追溯**，
就需要三张表：

  * ``skill_rollouts``  —— 一次灰度发布的**当前状态**与流量指针；
  * ``rollout_metrics`` —— 每个阶段窗口的**实测**指标快照（未测量列保持 NULL）；
  * ``skill_rollbacks`` —— **只追加**的回滚记录（红线 6 / 20：回滚写新行，
    已发布版本与历史记录**绝不删除或改写**）。

纪律（INC46 §8）
----------------
* 红线 5：``tenant_id TEXT NOT NULL`` 是每表第一列；无租户即 fail-closed（应用层拒绝写）。
* 红线 6 / 20：只 INSERT 演进；回滚 = 新增记录 + 指针切回，本表不做破坏性更新。
* 红线 20：改动**加性**（新表 + 新索引），不改既有列语义。
* 幂等：``CREATE TABLE/INDEX IF NOT EXISTS`` ⇒ ``upgrade head`` 连跑两次，第二次 no-op。

Revision ID: 032
Revises: 031
Create Date: 2026-10-04
"""

from alembic import op

revision = "032"
down_revision = "031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``gen_random_uuid()`` (the ``id`` default) lives in pgcrypto on older
    # servers; migration 010 already created it. Harmless no-op when present.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # --- skill_rollouts: one canary release's current state + traffic pointer --- #
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_rollouts (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            -- Opaque TEXT tenant (never coerced/collapsed) — fail-closed writes.
            tenant_id         TEXT NOT NULL,
            skill_id          TEXT NOT NULL,
            -- The candidate version under exposure (e.g. "1.1.0").
            candidate_version TEXT NOT NULL,
            -- The incumbent version the traffic pointer falls back to.
            incumbent_version TEXT NOT NULL,
            -- Current exposure of the candidate: one of 5 / 25 / 100.
            stage_pct         INTEGER NOT NULL,
            -- canary | promoted | rolled_back
            state             VARCHAR(16) NOT NULL DEFAULT 'canary',
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_skill_rollouts_state
                CHECK (state IN ('canary', 'promoted', 'rolled_back')),
            CONSTRAINT ck_skill_rollouts_stage
                CHECK (stage_pct IN (0, 5, 25, 100))
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_rollouts_tenant_skill "
        "ON skill_rollouts (tenant_id, skill_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_rollouts_state "
        "ON skill_rollouts (tenant_id, state, created_at DESC)"
    )

    # --- rollout_metrics: per-stage measured snapshot (unmeasured ⇒ NULL) --- #
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS rollout_metrics (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id           TEXT NOT NULL,
            rollout_id          UUID NOT NULL,
            stage_pct           INTEGER NOT NULL,
            -- Labeled (non-UNKNOWN) run count — the rate denominator (红线 12).
            labeled_runs        INTEGER NOT NULL DEFAULT 0,
            successes           INTEGER NOT NULL DEFAULT 0,
            -- Rates are NULL when unmeasured (红线 4) —— never a default 0.
            success_rate        DOUBLE PRECISION,
            success_rate_lb     DOUBLE PRECISION,
            validate_fail_rate  DOUBLE PRECISION,
            rework_rate         DOUBLE PRECISION,
            p95_latency_ms      DOUBLE PRECISION,
            cost                DOUBLE PRECISION,
            window_hours        DOUBLE PRECISION,
            measured_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT fk_rollout_metrics_rollout
                FOREIGN KEY (rollout_id) REFERENCES skill_rollouts (id)
                ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_rollout_metrics_rollout "
        "ON rollout_metrics (tenant_id, rollout_id, measured_at DESC)"
    )

    # --- skill_rollbacks: append-only rollback ledger (红线 6 / 20) --- #
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_rollbacks (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id     TEXT NOT NULL,
            rollout_id    UUID NOT NULL,
            from_version  TEXT NOT NULL,
            to_version    TEXT NOT NULL,
            -- success_rate_drop | validate_fail_rate_rise | p95_latency_ratio
            -- | dangerous_escalation | manual
            trigger       VARCHAR(32) NOT NULL,
            reason        TEXT,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT fk_skill_rollbacks_rollout
                FOREIGN KEY (rollout_id) REFERENCES skill_rollouts (id)
                ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_rollbacks_rollout "
        "ON skill_rollbacks (tenant_id, rollout_id, created_at DESC)"
    )


def downgrade() -> None:
    # Additive-only (§九): dropping the tables is the exact inverse.
    op.execute("DROP TABLE IF EXISTS skill_rollbacks")
    op.execute("DROP TABLE IF EXISTS rollout_metrics")
    op.execute("DROP TABLE IF EXISTS skill_rollouts")
