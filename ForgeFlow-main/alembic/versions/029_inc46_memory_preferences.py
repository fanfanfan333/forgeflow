"""029 - INC46 T31: ``memory_preferences`` (tenant / user durable memory).

A CLAUDE.md-style memory for the document-editing agent: the tenant and each
user may state durable preferences (writing style / glossary / banned words /
document conventions) which are injected into the next task's context.

Design notes (INC46 §九 + §T31)
-------------------------------
* **Additive, new table only** — no existing column or table is touched.
* **``tenant_id`` is ``TEXT NOT NULL``** (the migration-020 single-yardstick
  contract) and every index leads with it (红线 5). The application layer is
  fail-closed, so an unresolved tenant never writes and never reads.
* **``source`` is constrained to the three allowed values** by a ``CHECK`` so a
  row can never claim an unknown provenance; only ``explicit`` /
  ``confirmed_suggestion`` are effective (红线 14 — a document-derived
  ``suggestion`` is stored but never injected).
* **No ``0`` defaults on measured values** (红线 4). ``user_id`` / ``pref_key``
  are ``NULL``-able when not applicable; there is no numeric column to fake.

Ownership: ``019``=T01, ``020``=T02, ``021``=T05, ``022``=T15, ``023``=T10,
``024``=T16, ``025``=T21, ``026``=T32, ``027``=T17, ``028``=T22, this ``029``=T31
— per the §九 migration registry, so the chain stays strictly serial: 028 → 029.

Re-entrancy: ``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS`` —
``upgrade head`` twice is a no-op the second time. Importing this module opens
no connection.

Revision ID: 029
Revises: 028
Create Date: 2026-10-04
"""

from alembic import op

revision = "029"
down_revision = "028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``gen_random_uuid()`` lives in pgcrypto on older servers; migration 010
    # already created it. Harmless no-op when present.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_preferences (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            -- Opaque TEXT tenant (never coerced/collapsed) — fail-closed writes.
            tenant_id   TEXT NOT NULL,
            -- tenant | user
            scope       VARCHAR(16) NOT NULL,
            -- NULL for a tenant-level preference.
            user_id     TEXT,
            -- style | glossary | banned_term | doc_convention
            kind        VARCHAR(32) NOT NULL,
            -- override key: the glossary term, else '' (the kind is the key).
            pref_key    TEXT NOT NULL DEFAULT '',
            value       TEXT NOT NULL,
            -- explicit | confirmed_suggestion | suggestion
            source      VARCHAR(32) NOT NULL,
            created_by  TEXT NOT NULL DEFAULT '',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT memory_preferences_source_chk
                CHECK (source IN ('explicit', 'confirmed_suggestion', 'suggestion')),
            CONSTRAINT memory_preferences_scope_chk
                CHECK (scope IN ('tenant', 'user'))
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_preferences_tenant_scope "
        "ON memory_preferences (tenant_id, scope, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_preferences_tenant_user "
        "ON memory_preferences (tenant_id, user_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_memory_preferences_tenant_user")
    op.execute("DROP INDEX IF EXISTS idx_memory_preferences_tenant_scope")
    op.execute("DROP TABLE IF EXISTS memory_preferences")
