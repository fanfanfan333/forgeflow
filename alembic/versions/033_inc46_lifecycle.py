"""033 — INC46 T35: ``skill_lifecycle_events`` / ``skill_merge_proposals``。

Why these tables exist
----------------------
T09 在选择层预留了「检索前过滤 deprecated/archived」的闸（该过滤在状态出现前是
空操作）。T35 把 Skill 生命周期做成一个**真状态机**（``draft → candidate →
published → deprecated → archived``），于是需要：

  * ``skill_lifecycle_events`` —— **只追加**的状态迁移审计流（红线 6：历史不可改写）；
  * ``skill_merge_proposals``  —— 去重 / 合并**提案**（不自动合并）；一次提案保留
    两个**源 skill ID**（``source_skill_ids`` = 来源链），人工 approve 后落
    ``approved`` 并记录 ``merged_skill_id``。

纪律（INC46 §8）
----------------
* 红线 5：``tenant_id TEXT NOT NULL`` 是每表第一列；无租户即 fail-closed（应用层拒写）。
* 红线 6：生命周期事件只 INSERT；合并**产生新 skill**，不覆盖既有版本。
* 红线 20：改动**加性**（新表 + 新索引），不改既有列语义。
* 幂等：``CREATE TABLE/INDEX IF NOT EXISTS`` ⇒ ``upgrade head`` 连跑两次，第二次 no-op。

Revision ID: 033
Revises: 032
Create Date: 2026-10-04
"""

from alembic import op

revision = "033"
down_revision = "032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``gen_random_uuid()`` (the ``id`` default) lives in pgcrypto on older
    # servers; migration 010 already created it. Harmless no-op when present.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # --- skill_lifecycle_events: append-only state-transition audit ledger --- #
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_lifecycle_events (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            -- Opaque TEXT tenant (never coerced/collapsed) — fail-closed writes.
            tenant_id   TEXT NOT NULL,
            skill_id    TEXT NOT NULL,
            -- draft | candidate | published | deprecated | archived
            from_state  VARCHAR(16) NOT NULL,
            to_state    VARCHAR(16) NOT NULL,
            reason      TEXT,
            actor       TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_skill_lifecycle_from
                CHECK (from_state IN ('draft', 'candidate', 'published', 'deprecated', 'archived')),
            CONSTRAINT ck_skill_lifecycle_to
                CHECK (to_state IN ('draft', 'candidate', 'published', 'deprecated', 'archived'))
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_lifecycle_tenant_skill "
        "ON skill_lifecycle_events (tenant_id, skill_id, created_at DESC)"
    )

    # --- skill_merge_proposals: dedup/merge proposals (source chain preserved) --- #
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_merge_proposals (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id         TEXT NOT NULL,
            primary_skill_id  TEXT NOT NULL,
            duplicate_skill_id TEXT NOT NULL,
            -- The two source skill ids (the merge provenance chain).
            source_skill_ids  JSONB NOT NULL DEFAULT '[]'::jsonb,
            -- Measured trigger values (NULL when unmeasured — 红线 4).
            description_cosine DOUBLE PRECISION,
            tool_jaccard       DOUBLE PRECISION,
            -- proposed | approved | rejected
            status            VARCHAR(16) NOT NULL DEFAULT 'proposed',
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            decided_by        TEXT,
            decided_at        TIMESTAMPTZ,
            merged_skill_id   TEXT,
            CONSTRAINT ck_skill_merge_status
                CHECK (status IN ('proposed', 'approved', 'rejected'))
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_merge_proposals_tenant_status "
        "ON skill_merge_proposals (tenant_id, status, created_at DESC)"
    )


def downgrade() -> None:
    # Additive-only (§九): dropping the tables is the exact inverse.
    op.execute("DROP TABLE IF EXISTS skill_merge_proposals")
    op.execute("DROP TABLE IF EXISTS skill_lifecycle_events")
