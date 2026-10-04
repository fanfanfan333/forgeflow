"""031 — INC46 T25: ``artifact_version_edges``（多轮迭代与版本链的 DAG 边）。

Why this table exists
---------------------
T22 已经给了 ``artifact_versions``（含 ``base_version`` 整数），足以表达「这条
pending 编辑派生自哪一版」。但 T25 的版本链有两种边语义，单靠一个整数列表达不了：

  * ``refine`` —— 追问（「再正式一点」）基于**最近一个 committed 版本**继续；
  * ``revert`` —— 回退到某一版：**新产出的版本内容 == 目标版本**，而它的
    ``parent`` 是当时的 head，``source`` 才是被回退到的那个版本。

把 ``base_version`` 同时当作 parent 与 source 会丢掉「revert 的来源」这一事实
（从而无法区分「正常迭代」与「回退」）。故新增一张**加性**边表：
``child_version → parent_version`` + ``edge_kind`` + （revert 时的）``source_version``。
历史行零改动，旧行为完全不变（缺边 = 该版本没有登记的链关系）。

纪律（INC46 §8）
----------------
* 红线 5：``tenant_id TEXT NOT NULL`` 是每行第一列；无租户即 fail-closed（应用层拒绝写）。
* 红线 6：**不删除、不改写历史** —— 本表只 INSERT（revert 也只追加新行）。
* 红线 20：改动的集合是**加性**的（新表 + 新索引），不改既有列语义。
* 幂等：``CREATE TABLE/INDEX IF NOT EXISTS`` ⇒ ``upgrade head`` 连跑两次，第二次 no-op。

Revision ID: 031
Revises: 030
Create Date: 2026-10-04
"""

from alembic import op

revision = "031"
down_revision = "030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``gen_random_uuid()`` (the ``id`` default) lives in pgcrypto on older
    # servers; migration 010 already created it. Harmless no-op when present.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS artifact_version_edges (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            -- Opaque TEXT tenant (never coerced/collapsed) — fail-closed writes.
            tenant_id       TEXT NOT NULL,
            -- Opaque logical artefact (document) id.
            artifact_id     TEXT NOT NULL,
            -- The version this edge points AT (the child in the chain).
            child_version   INTEGER NOT NULL,
            -- The version it was derived FROM. NULL only for the root (v1).
            parent_version  INTEGER,
            -- initial | refine | revert
            edge_kind       VARCHAR(16) NOT NULL,
            -- For ``revert``: the version whose content was restored (the semantic
            -- source). NULL for ``initial`` / ``refine`` (未测量 ⇒ NULL, 红线 4).
            source_version  INTEGER,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            -- One edge per child version (the UPSERT target).
            CONSTRAINT uq_artifact_version_edges_child
                UNIQUE (tenant_id, artifact_id, child_version),
            -- The vocabulary is fixed; an unknown kind is a writer bug, not data.
            CONSTRAINT ck_artifact_version_edges_kind
                CHECK (edge_kind IN ('initial', 'refine', 'revert'))
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_artifact_version_edges_child "
        "ON artifact_version_edges (tenant_id, artifact_id, child_version)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_artifact_version_edges_parent "
        "ON artifact_version_edges (tenant_id, artifact_id, parent_version)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_artifact_version_edges_kind "
        "ON artifact_version_edges (tenant_id, edge_kind, created_at DESC)"
    )


def downgrade() -> None:
    # Additive-only (§九): dropping the table is the exact inverse.
    op.execute("DROP TABLE IF EXISTS artifact_version_edges")
