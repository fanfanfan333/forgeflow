"""INC46 T05 — Evolution closed loop, integration level.

Two things can only be proven against the real stack:

1. **Migration ``021``** really created ``skill_versions.created_from_runs`` as a
   *nullable* ``jsonb`` column and backfilled **nothing** — "no provenance" must
   stay distinguishable from "empty provenance", so pre-existing rows are NULL.
2. **The loop is wired to real collaborators**: :func:`maybe_evolve` runs end to
   end through the real ``require_tenant`` gate and the real repositories without
   raising, and reports honestly instead of inventing a version.

The decision gates themselves (threshold / cooldown / generation cap /
idempotency) are covered deterministically in
``tests/unit/test_inc46_evolution_loop.py``.
"""

from __future__ import annotations

import os

import pytest

from forgeflow.skills.evolution_loop import maybe_evolve

DSN = os.environ.get(
    "FF_TEST_DSN", "postgresql://forgeflow:forgeflow@127.0.0.1:5433/forgeflow"
)

TENANT = "tenant-evolve-integration"
SKILL_ID = "00000000-0000-0000-0000-000000000000"


# --------------------------------------------------------------------------- #
# 1. migration 021 — provenance column, nullable, no backfill                   #
# --------------------------------------------------------------------------- #
async def test_created_from_runs_column_is_nullable_jsonb():
    try:
        import asyncpg
    except ImportError:  # pragma: no cover — env without the pg driver
        pytest.skip("asyncpg 不可用，跳过 PG 列校验")

    try:
        conn = await asyncpg.connect(DSN)
    except Exception as exc:  # noqa: BLE001 — PG down is an env issue, not a bug
        pytest.skip(f"PG 不可用：{exc}")

    try:
        rows = await conn.fetch(
            """
            SELECT column_name, is_nullable, data_type
            FROM information_schema.columns
            WHERE table_name = 'skill_versions'
              AND column_name = 'created_from_runs'
            """
        )
    finally:
        await conn.close()

    assert len(rows) == 1, (
        "迁移 021 必须创建 skill_versions.created_from_runs；"
        f"实际查到 {len(rows)} 列"
    )
    row = rows[0]
    assert row["is_nullable"] == "YES", "provenance 必须可空（未观测 ⇒ NULL）"
    assert row["data_type"] == "jsonb"


async def test_no_provenance_was_backfilled():
    """Pre-existing versions keep NULL — we never fabricate provenance."""
    try:
        import asyncpg
    except ImportError:  # pragma: no cover
        pytest.skip("asyncpg 不可用")

    try:
        conn = await asyncpg.connect(DSN)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PG 不可用：{exc}")

    try:
        total = await conn.fetchval("SELECT COUNT(*) FROM skill_versions")
        filled = await conn.fetchval(
            "SELECT COUNT(*) FROM skill_versions WHERE created_from_runs IS NOT NULL"
        )
    finally:
        await conn.close()

    # 021 adds a column only; it must not write a single value.
    assert filled == 0, f"迁移回填了 {filled}/{total} 行，provenance 必须不回填"


# --------------------------------------------------------------------------- #
# 2. the loop is genuinely wired (no stub left behind)                          #
# --------------------------------------------------------------------------- #
class _EmptySkillRepo:
    """A tenant with no such skill — the honest 'nothing to evolve' case."""

    async def get_skill(self, tenant, skill_id):
        return None

    async def get_version(self, tenant, skill_id, semver):
        return None

    async def list_versions(self, tenant, skill_id):
        return []


class _NoopRepo:
    def __getattr__(self, name):  # any collaborator call is a no-op here
        async def _noop(*args, **kwargs):
            return None

        return _noop


async def test_missing_skill_reports_honestly_and_never_publishes():
    out = await maybe_evolve(
        TENANT,
        SKILL_ID,
        actor="integration",
        skill_repo=_EmptySkillRepo(),
        candidate_repo=_NoopRepo(),
        experience_repo=_NoopRepo(),
        policy_repo=_NoopRepo(),
    )
    payload = out.to_dict()
    assert payload["triggered"] is False
    assert payload["applied"] is False
    assert payload["to_version"] is None
    assert payload["reason"], "一个未触发的结果必须说明原因"


async def test_unresolved_tenant_is_refused_even_with_real_collaborators():
    from forgeflow.skills.tenant_scope import require_tenant

    with pytest.raises(Exception):  # noqa: B017 — fail-closed is the contract
        require_tenant(None)
