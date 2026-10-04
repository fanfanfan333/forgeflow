"""030 - INC46 T33: ``experience_quarantine`` (注入 / 污染隔离区).

红线 14 — 文档 / 工具输出里冒充指令的文字不得写入经验 / Skill / 记忆。写路径
（T32 脱敏之后）命中指令性文本的 Experience 被整体转入本表等待人工复核；复核
放行后其 payload 才被重新写回 ``experiences``（迁移 ``026`` 的 ``scrub_*`` 列仍是
Miner 准入闸门）。

Design notes (INC46 §九 + §T33)
-------------------------------
* **Additive, new table only** — no existing column or table is touched.
* **``tenant_id`` is ``TEXT NOT NULL``** (the migration-020 single-yardstick
  contract) and every index leads with it (红线 5). The application layer is
  fail-closed, so an unresolved tenant never writes and never reads.
* **``status`` is constrained** to ``quarantined`` / ``released`` by a ``CHECK``
  so a row can never claim an unknown lifecycle state.
* **``UNIQUE (tenant_id, experience_id)``** makes the write path's upsert
  idempotent: re-quarantining the same experience replaces its row rather than
  piling up duplicates.
* **Minimisation (同 ``scrub_audits``)** — ``findings`` holds only tags + short
  excerpts (never the raw flagged text); ``content_sha256`` proves two states
  differ without holding the bytes.
* **No ``0`` defaults on measured values** (红线 4). ``released_at`` is
  ``NULL`` until a human releases (未测量 ⇒ NULL); ``content_sha256`` / ``run_id``
  are honest empties, never ``0``.

Ownership: ``019``=T01, ``020``=T02, ``021``=T05, ``022``=T15, ``023``=T10,
``024``=T16, ``025``=T21, ``026``=T32, ``027``=T17, ``028``=T22, ``029``=T31,
this ``030``=T33 — per the §九 migration registry, so the chain stays strictly
serial: 029 → 030.

Re-entrancy: ``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS`` —
``upgrade head`` twice is a no-op the second time. Importing this module opens
no connection.

Revision ID: 030
Revises: 029
Create Date: 2026-10-04
"""

from alembic import op

revision = "030"
down_revision = "029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``gen_random_uuid()`` lives in pgcrypto on older servers; migration 010
    # already created it. Harmless no-op when present.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS experience_quarantine (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            -- Opaque TEXT tenant (never coerced/collapsed) — fail-closed writes.
            tenant_id         TEXT NOT NULL,
            -- Opaque experience id (TEXT — no UUID coercion of the hub id).
            experience_id     TEXT NOT NULL,
            run_id            TEXT NOT NULL DEFAULT '',
            -- quarantined | released
            status            VARCHAR(16) NOT NULL,
            -- comma-joined detector tags (e.g. "instruction_override,amount_override")
            reason            TEXT NOT NULL DEFAULT '',
            -- Per-finding tags + short excerpts. Holds NO raw flagged text.
            findings          JSONB NOT NULL DEFAULT '[]',
            -- The experience to re-admit on release (already scrubbed by T32).
            payload           JSONB NOT NULL DEFAULT '{}',
            -- SHA-256 of the flagged free text — provenance without the bytes.
            content_sha256    TEXT,
            -- Detector vocabulary revision that quarantined this row.
            detector_version  VARCHAR(16) NOT NULL DEFAULT '1',
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            -- NULL = not released yet (未测量 ⇒ NULL，红线 4).
            released_at       TIMESTAMPTZ,
            released_by       TEXT NOT NULL DEFAULT '',
            CONSTRAINT experience_quarantine_status_chk
                CHECK (status IN ('quarantined', 'released')),
            CONSTRAINT experience_quarantine_tenant_exp_uniq
                UNIQUE (tenant_id, experience_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_experience_quarantine_tenant_status "
        "ON experience_quarantine (tenant_id, status, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_experience_quarantine_tenant_experience "
        "ON experience_quarantine (tenant_id, experience_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_experience_quarantine_tenant_experience")
    op.execute("DROP INDEX IF EXISTS idx_experience_quarantine_tenant_status")
    op.execute("DROP TABLE IF EXISTS experience_quarantine")
