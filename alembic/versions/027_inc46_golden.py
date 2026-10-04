"""027 - INC46 T17: 独立 Golden 回归集（Held-out Eval Registry）。

任务书 §九 迁移登记：``T16=024, T21=025, T32=026, T17=027``，故本迁移
``revision="027"`` / ``down_revision="026"``，链条保持严格串行 026 → 027。

引入三张表（评测集与训练/挖掘数据隔离的落库形态）
--------------------------------------------------
* ``golden_sets`` —— 一个 golden 集：``provenance``（``human_curated`` /
  ``customer_approved_export``）、``content_hash``（一旦 frozen 即锁定内容）、
  ``frozen`` / ``frozen_at``、train/holdout 计数、可选的 ``baseline_metrics``。
* ``golden_cases`` —— 每个用例一行：``source_doc_fingerprint``（泄漏检查的键）、
  ``run_id``、``split``（train/holdout）、``provenance``。级联随集合删除。
* ``golden_runs`` —— 每次 golden 回归一行：``set_content_hash``（可追溯到集合
  内容）、``passed``（**NULLABLE**：未测量 ⇒ NULL，红线 4）、``voided``（泄漏作废）、
  ``leakage`` 报告。

租户纪律（红线 5）
------------------
``tenant_id`` 为不透明 ``TEXT NOT NULL``（迁移 020 的单一标尺口径），每个索引以
``tenant_id`` 打头；应用层 fail-closed，未解析租户既不写也不读（读回空集）。

Re-entrancy
-----------
``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS`` —— ``upgrade head``
跑两次第二次为空操作。导入本模块不建立任何连接。

Revision ID: 027
Revises: 026
Create Date: 2026-10-05
"""

from alembic import op

revision = "027"
down_revision = "026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``gen_random_uuid()`` (the ``id`` default) lives in pgcrypto on older
    # servers; migration 010 already created it. Harmless no-op when present.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # --- the frozen, hash-locked golden set --------------------------------- #
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS golden_sets (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            -- Opaque TEXT tenant (never coerced/collapsed) — fail-closed writes.
            tenant_id        TEXT NOT NULL,
            name             TEXT NOT NULL,
            -- human_curated | customer_approved_export (mined 不得入库)
            provenance       VARCHAR(32) NOT NULL
                             CHECK (provenance IN
                                    ('human_curated', 'customer_approved_export')),
            -- The frozen content lock (红线：改内容 ⇒ 哈希不符 ⇒ 拒绝).
            content_hash     TEXT NOT NULL,
            case_count       INTEGER NOT NULL DEFAULT 0,
            train_count      INTEGER NOT NULL DEFAULT 0,
            holdout_count    INTEGER NOT NULL DEFAULT 0,
            -- Optional recorded baseline (empty ⇒ regression is "no_baseline").
            baseline_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
            frozen           BOOLEAN NOT NULL DEFAULT TRUE,
            -- NULL 只在极旧/异常行出现：未冻结 ⇒ 未测量 ⇒ NULL (红线 4).
            frozen_at        TIMESTAMPTZ,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_golden_sets_tenant_created "
        "ON golden_sets (tenant_id, created_at DESC)"
    )
    # A tenant may not hold two frozen sets under the same name (idempotency key
    # for re-import: same name + same content hash ⇒ returns the existing row).
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_golden_sets_tenant_name "
        "ON golden_sets (tenant_id, name)"
    )

    # --- one row per golden case ------------------------------------------- #
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS golden_cases (
            id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            set_id                 UUID NOT NULL
                                   REFERENCES golden_sets(id) ON DELETE CASCADE,
            tenant_id              TEXT NOT NULL,
            case_id                TEXT NOT NULL,
            -- The leakage key: source document fingerprint ("sha256:<hex>").
            source_doc_fingerprint TEXT NOT NULL,
            run_id                 TEXT,
            instruction            TEXT,
            expected_summary       TEXT,
            -- train | holdout
            split                  VARCHAR(8) NOT NULL DEFAULT 'train'
                                   CHECK (split IN ('train', 'holdout')),
            provenance             VARCHAR(32) NOT NULL
                                   CHECK (provenance IN
                                          ('human_curated', 'customer_approved_export')),
            created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (set_id, case_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_golden_cases_set "
        "ON golden_cases (set_id, case_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_golden_cases_tenant_fingerprint "
        "ON golden_cases (tenant_id, source_doc_fingerprint)"
    )

    # --- one row per golden regression run --------------------------------- #
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS golden_runs (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id        TEXT NOT NULL,
            set_id           UUID REFERENCES golden_sets(id) ON DELETE SET NULL,
            -- The set content hash this run is traceable to (回归结果可追溯).
            set_content_hash TEXT NOT NULL,
            skill_id         TEXT,
            candidate_id     TEXT,
            train_size       INTEGER NOT NULL DEFAULT 0,
            holdout_size     INTEGER NOT NULL DEFAULT 0,
            -- NULLABLE: 未测量 (no baseline) ⇒ NULL，绝不写 0/False (红线 4).
            passed           BOOLEAN,
            -- TRUE ⇒ 该候选回归因数据泄漏而作废.
            voided           BOOLEAN NOT NULL DEFAULT FALSE,
            leakage          JSONB NOT NULL DEFAULT '{}'::jsonb,
            metrics          JSONB NOT NULL DEFAULT '{}'::jsonb,
            reason           TEXT,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_golden_runs_tenant_set "
        "ON golden_runs (tenant_id, set_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_golden_runs_tenant_skill "
        "ON golden_runs (tenant_id, skill_id, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_golden_runs_tenant_skill")
    op.execute("DROP INDEX IF EXISTS idx_golden_runs_tenant_set")
    op.execute("DROP TABLE IF EXISTS golden_runs")
    op.execute("DROP INDEX IF EXISTS idx_golden_cases_tenant_fingerprint")
    op.execute("DROP INDEX IF EXISTS idx_golden_cases_set")
    op.execute("DROP TABLE IF EXISTS golden_cases")
    op.execute("DROP INDEX IF EXISTS uq_golden_sets_tenant_name")
    op.execute("DROP INDEX IF EXISTS idx_golden_sets_tenant_created")
    op.execute("DROP TABLE IF EXISTS golden_sets")
