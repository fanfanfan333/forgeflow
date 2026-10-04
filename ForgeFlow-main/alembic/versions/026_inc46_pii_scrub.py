"""026 - INC46 T32: ``experiences.scrub_status`` + ``scrub_version`` + ``scrub_audits``.

Red line 13 — user-document content must be scrubbed **before** it becomes an
Experience (and before the pattern miner reads it). This migration adds the two
**additive, nullable** bookkeeping columns the write path stamps, plus the
minimised audit trail.

Additive columns on ``experiences``
-----------------------------------
* ``scrub_status``  VARCHAR(16) — ``'scrubbed'`` / ``'refused'``. **NULLABLE**:
  ``NULL`` means *never scrubbed* (未测量 ⇒ NULL, 红线 4). It is deliberately
  **not** defaulted — a legacy row must read back as ``NULL`` (not scrubbed), and
  the miner treats ``NULL`` as **not eligible** (fail-closed, 红线 13).
  Existing rows are therefore left untouched (no backfill in the migration; the
  ``forgeflow.jobs.backfill_scrub`` job does that explicitly).
* ``scrub_version`` VARCHAR(16) — the scrubber algorithm version. **NULLABLE**,
  no default, for the same honesty reason.

New table ``scrub_audits`` (溯源可用但最小化)
--------------------------------------------
One row per scrub operation. It stores **no raw PII** — only provenance: the
tenant, the experience id, the version transition, the categories that were hit,
a match count, a changed flag and a ``content_sha256`` of the *scrubbed* text.
``from_version`` is ``NULL`` for a first scrub (never measured before).

Ownership: migration ``019`` = T01, ``020`` = T02, ``021`` = T05, ``022`` = T15,
``023`` = T10, ``024`` = T16, ``025`` = T21, this ``026`` = T32 — per the §九
migration registry (T16=024, T21=025, T32=026, T17=027), so the chain stays
strictly serial: 025 → 026.

Tenant discipline: ``scrub_audits.tenant_id`` is opaque ``TEXT NOT NULL`` (the
migration-020 single-yardstick contract); every index leads with ``tenant_id``.
The application layer fails closed, so an unresolved tenant never writes and
never reads (its reads return the empty set).

Re-entrancy: ``ADD COLUMN IF NOT EXISTS`` + ``CREATE TABLE IF NOT EXISTS`` +
``CREATE INDEX IF NOT EXISTS`` — ``upgrade head`` twice is a no-op the second
time. Importing this module opens no connection.

Revision ID: 026
Revises: 025
Create Date: 2026-10-04
"""

from alembic import op

revision = "026"
down_revision = "025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``gen_random_uuid()`` (the ``id`` default) lives in pgcrypto on older
    # servers; migration 010 already created it. Harmless no-op when present.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # --- additive, nullable bookkeeping columns (never defaulted, 红线 4) --- #
    op.execute(
        "ALTER TABLE experiences ADD COLUMN IF NOT EXISTS scrub_status VARCHAR(16)"
    )
    op.execute(
        "ALTER TABLE experiences ADD COLUMN IF NOT EXISTS scrub_version VARCHAR(16)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_experiences_tenant_scrub "
        "ON experiences (tenant_id, scrub_status)"
    )

    # --- the minimised audit trail --- #
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS scrub_audits (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            -- Opaque TEXT tenant (never coerced/collapsed) — fail-closed writes.
            tenant_id       TEXT NOT NULL,
            -- Opaque experience id (TEXT — no UUID coercion of the hub id).
            experience_id   TEXT NOT NULL,
            -- NULL = never scrubbed before (未测量 ⇒ NULL，红线 4).
            from_version    VARCHAR(16),
            to_version      VARCHAR(16) NOT NULL,
            -- scrubbed | refused
            status          VARCHAR(16) NOT NULL,
            -- Categories that were hit. Holds category NAMES, never PII values.
            categories      JSONB NOT NULL DEFAULT '[]',
            match_count     INTEGER NOT NULL DEFAULT 0,
            changed         BOOLEAN NOT NULL DEFAULT FALSE,
            -- SHA-256 of the *scrubbed* content — provenance without the bytes.
            content_sha256  TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_scrub_audits_tenant_experience "
        "ON scrub_audits (tenant_id, experience_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_scrub_audits_tenant_status "
        "ON scrub_audits (tenant_id, status, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_scrub_audits_tenant_status")
    op.execute("DROP INDEX IF EXISTS idx_scrub_audits_tenant_experience")
    op.execute("DROP TABLE IF EXISTS scrub_audits")
    op.execute("DROP INDEX IF EXISTS idx_experiences_tenant_scrub")
    op.execute("ALTER TABLE experiences DROP COLUMN IF EXISTS scrub_version")
    op.execute("ALTER TABLE experiences DROP COLUMN IF EXISTS scrub_status")
