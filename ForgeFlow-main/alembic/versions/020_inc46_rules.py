"""020 - INC46 T02: ``skill_rule_suggestions`` (the deterministic Rule asset table).

Adds one additive table that persists the MUST / MUST_NOT rules T02 extracts from
the real execution trace (INC46 §1.2 R3 / §2.2). It is created **by T02** (not by
T01) per the delivery ruling **D-R1**: migration ``019`` owns only the T01
``run_steps`` change; the rule table lives here and chains off it
(``down_revision = "019"``).

Why a single flat table (no separate kind tables): a rule is one row of
(tenant, pattern, kind, templated text, support, confidence, evidence run ids).
``rule_kind`` ∈ ``must`` / ``must_not`` (``rule_assets.RULE_KINDS``); ``status``
is a lifecycle label (``suggested`` …) kept as an opaque string, exactly like the
``skills`` / ``skill_candidates`` status columns already are.

Tenant discipline: ``tenant_id`` is opaque ``TEXT NOT NULL`` (single-yardstick
contract, migration 013). The application layer fails closed — an unresolved
tenant never writes and never reads — so the column is ``NOT NULL`` while every
row still round-trips the exact opaque tenant string.

Idempotency: ``UNIQUE (tenant_id, pattern_key, rule_kind, rule_text)`` lets
``persist_rules`` use ``ON CONFLICT DO NOTHING`` so re-running extraction never
duplicates a rule.

Re-entrancy: ``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS`` —
``upgrade head`` twice is a no-op the second time. Importing this module opens no
connection; it only declares the upgrade/downgrade bodies.

Revision ID: 020
Revises: 019
Create Date: 2026-10-03
"""

from alembic import op

revision = "020"
down_revision = "019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``gen_random_uuid()`` (the ``id`` default) lives in pgcrypto on older
    # servers; migration 010 already created it. Harmless no-op when present.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_rule_suggestions (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            -- Opaque TEXT tenant (never coerced/collapsed) — fail-closed writes.
            tenant_id      TEXT NOT NULL,
            pattern_key    TEXT NOT NULL DEFAULT '',
            rule_kind      VARCHAR(16) NOT NULL,
            rule_text      TEXT NOT NULL,
            -- Recomputable evidence: support = the rule's evidence base,
            -- confidence = the recomputed rate.
            support        INTEGER NOT NULL DEFAULT 0,
            confidence     DOUBLE PRECISION NOT NULL DEFAULT 0,
            source_run_ids TEXT[] NOT NULL DEFAULT '{}',
            status         VARCHAR(16) NOT NULL DEFAULT 'suggested',
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            -- Makes persist_rules idempotent (ON CONFLICT DO NOTHING).
            UNIQUE (tenant_id, pattern_key, rule_kind, rule_text)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_rule_suggestions_tenant "
        "ON skill_rule_suggestions (tenant_id, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS skill_rule_suggestions")
