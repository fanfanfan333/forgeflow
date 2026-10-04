"""028 - INC46 T22: Diff 预览与确认（含修订模式）。

任务书 §九 迁移登记：``T16=024, T21=025, T32=026, T17=027, T22=028``，故本迁移
``revision="028"`` / ``down_revision="027"``，链条保持严格串行 027 → 028。

引入/扩展的落库形态
--------------------
* ``artifact_versions``（**新表**）—— 文档产物的一个「版本」行。T22 的核心不是
  普通版本链，而是「先产出 ``pending`` 版本 + diff，用户确认后才提交
  ``committed`` 版本」。因此本表的 ``state`` 列取
  ``pending | committed | rejected | expired``，**历史默认 ``committed``**
  （任务书原文：``artifact_versions 增加 state 列（加性，历史默认 committed）``）。

  本仓此前**没有** ``artifact_versions`` 表（书面差异见交付报告）：任务书假设该表
  已存在，实际 NOT_FOUND。为同时满足「新增表」与「加性增加 state 列」两条，本迁移
  分两步：先 ``CREATE TABLE IF NOT EXISTS`` 建出**不含** ``state`` 的基线表，再
  ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS state ... DEFAULT 'committed'`` ——
  于是「加性加列 + 历史默认 committed」在**任何**环境（表是否已存在）都成立且幂等。

* ``artifact_reviews``（**新表**）—— 一次「待人工确认」的评审记录（公开
  ``approval_id``）。状态机 ``pending → approved(committed) | rejected | expired``；
  reject 可附 ``reason``（写入 T16 的 ``feedback_events``）；``out_of_region``（区间
  外变化）非空时**阻断 approve**，除非 ``override``。

诚实纪律（INC46 §8 红线 4 — 未测量 ⇒ NULL，绝不 0/''）
-----------------------------------------------------
* ``out_of_region`` / ``diff`` / ``reason`` / ``override_reason`` / ``resolved_by`` /
  ``resolved_at`` / ``committed_at`` / ``expires_at`` 均为可空：未测量 / 未发生 ⇒ NULL。
* ``content_sha256`` / ``content_ref`` 仅在确有字节时写入。

租户纪律（红线 5）：``tenant_id`` 为不透明 ``TEXT NOT NULL``（迁移 020 的单一标尺
口径），每个索引以 ``tenant_id`` 打头；应用层 fail-closed，未解析租户既不写也不读。

Re-entrancy：``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS`` +
``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` —— ``upgrade head`` 跑两次第二次为空操作。
导入本模块不建立任何连接。

Revision ID: 028
Revises: 027
Create Date: 2026-10-05
"""

from alembic import op

revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``gen_random_uuid()`` (the ``id`` default) lives in pgcrypto on older
    # servers; migration 010 already created it. Harmless no-op when present.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # --- artifact_versions: the baseline table (no ``state`` yet) ----------- #
    # Created WITHOUT ``state`` on purpose so the very next statement can be the
    # additive ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS state`` the task book
    # names — the two together reconcile "new table" with "additive column with
    # historical default committed" in every environment.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS artifact_versions (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            -- Opaque TEXT tenant (never coerced/collapsed) — fail-closed writes.
            tenant_id       TEXT NOT NULL,
            -- Opaque logical artefact (document) id.
            artifact_id     TEXT NOT NULL,
            -- Monotonic per (tenant, artifact), starting at 1.
            version         INTEGER NOT NULL,
            -- The version this pending edit was derived from (NULL for v1).
            base_version    INTEGER,
            -- Content-addressed blob reference (documents.store, outside the tree).
            content_ref     TEXT,
            -- The deliverable bytes' digest (byte-identity / tamper check).
            content_sha256  TEXT,
            -- docx | pptx | xlsx | text | pdf
            format          VARCHAR(16) NOT NULL DEFAULT 'docx',
            -- The diff preview (paragraph / run / table-cell level). NULL when
            -- the diff was never computed (未测量 ⇒ NULL, 红线 4).
            diff            JSONB,
            -- Changes that fall OUTSIDE the edit's target region. NULL = not
            -- measured; [] = measured and clean; non-empty = blocks approve.
            out_of_region   JSONB,
            -- The owning review's public ``approval_id`` (NULL until reviewed).
            review_id       TEXT,
            -- The run this edit belongs to (NULL when unknown — never '').
            run_id          TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            -- Set only when the version really became committed.
            committed_at    TIMESTAMPTZ,
            -- One version number per (tenant, artifact) — the UPSERT target.
            CONSTRAINT uq_artifact_versions_tenant_artifact_version
                UNIQUE (tenant_id, artifact_id, version)
        )
        """
    )
    # Additive column with the historical default — the task-book contract.
    op.execute(
        "ALTER TABLE artifact_versions "
        "ADD COLUMN IF NOT EXISTS state VARCHAR(16) NOT NULL DEFAULT 'committed'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_artifact_versions_tenant_artifact "
        "ON artifact_versions (tenant_id, artifact_id, version DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_artifact_versions_tenant_state "
        "ON artifact_versions (tenant_id, state, created_at DESC)"
    )

    # --- artifact_reviews: the "waiting for a human" review record ---------- #
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS artifact_reviews (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       TEXT NOT NULL,
            -- Opaque per-tenant review id (the public ``approval_id``).
            approval_id     TEXT NOT NULL,
            artifact_id     TEXT NOT NULL,
            version         INTEGER NOT NULL,
            run_id          TEXT,
            -- pending | approved | rejected | expired
            status          VARCHAR(16) NOT NULL DEFAULT 'pending',
            -- The diff preview + out-of-region facts echoed onto the review row.
            diff            JSONB,
            out_of_region   JSONB,
            -- The human's reject reason. NULL until a reject supplies one.
            reason          TEXT,
            -- Whether out-of-region changes were overridden (NULL = not asked).
            override        BOOLEAN,
            override_reason TEXT,
            expires_at      TIMESTAMPTZ,
            resolved_at     TIMESTAMPTZ,
            resolved_by     TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_artifact_reviews_tenant_approval
                UNIQUE (tenant_id, approval_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_artifact_reviews_tenant_status "
        "ON artifact_reviews (tenant_id, status, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_artifact_reviews_tenant_artifact "
        "ON artifact_reviews (tenant_id, artifact_id, version)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_artifact_reviews_tenant_artifact")
    op.execute("DROP INDEX IF EXISTS idx_artifact_reviews_tenant_status")
    op.execute("DROP TABLE IF EXISTS artifact_reviews")
    op.execute("DROP INDEX IF EXISTS idx_artifact_versions_tenant_state")
    op.execute("DROP INDEX IF EXISTS idx_artifact_versions_tenant_artifact")
    op.execute("DROP TABLE IF EXISTS artifact_versions")
