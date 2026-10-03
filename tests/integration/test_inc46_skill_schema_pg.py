"""INC46 T10 (pg-档) — the eleven skill-contract tables on **real PostgreSQL**.

Proves, against the live dev PostgreSQL, what a mocked pool cannot:

  * migration ``023`` is **idempotent** (``alembic upgrade head`` twice; the
    second run is a clean no-op) and creates the eleven tables the task book
    names, extending the pre-existing ``skill_evaluations`` additively;
  * every one of the eleven tables accepts a write and reads back **tenant
    scoped** — a second tenant observes an empty set (row-level isolation);
  * an **unmeasured** number is stored and read back as ``NULL`` — never a
    fabricated ``0`` (red line 4);
  * the tenant predicate is **load-bearing**: the counterfactual probe drops it
    and the isolation invariant flips to a cross-tenant leak (so the isolation
    cases above are not vacuous).

It dials the same dev database the rest of the postgres-profile suite uses
(``localhost:5433``) and skips cleanly when it is unreachable, mirroring
``tests/integration/test_inc46_trace_pg.py``.
"""

from __future__ import annotations

import pathlib
import socket
import subprocess
import sys
import uuid

import asyncpg
import pytest

from forgeflow.config import get_settings
from forgeflow.repositories.postgres.skill_schema_repo import PgSkillSchemaRepository
from forgeflow.repositories.skill_schema_repo import (
    SKILL_SCHEMA_TABLES,
    SkillEvaluationEntry,
    SkillExample,
    SkillExperience,
    SkillFeedback,
    SkillKnowledgeRef,
    SkillPermission,
    SkillPolicy,
    SkillStep,
    SkillTestCase,
    SkillTestRun,
    SkillTool,
)

# Captured at import (before conftest's autouse DNS stub) so we reach a real
# 127.0.0.1 rather than the stubbed public IP.
_REAL_GETADDRINFO = socket.getaddrinfo
_DSN = get_settings().postgres_sync_url.replace("postgresql+psycopg://", "postgresql://")
_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Two distinct, non-UUID tenants — the sharpest test of real string isolation.
_TENANT_A = f"t-t10-a-{uuid.uuid4().hex[:8]}"
_TENANT_B = f"t-t10-b-{uuid.uuid4().hex[:8]}"

#: The eleven tables the task book fixes by name.
_EXPECTED_TABLES = frozenset(SKILL_SCHEMA_TABLES)


@pytest.fixture
async def pool(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)
    from forgeflow.database import _init_connection

    try:
        p = await asyncpg.create_pool(
            _DSN, min_size=1, max_size=3, timeout=4, init=_init_connection
        )
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"live Postgres not reachable ({exc})")
    yield p
    await p.close()


def _upgrade_head() -> None:
    """Run ``alembic upgrade head`` in a subprocess (brings 023 into the DB)."""
    import os

    child_env = dict(os.environ)
    child_env["POSTGRES_SYNC_URL"] = get_settings().postgres_sync_url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(_ROOT),
        env=child_env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade head failed:\n{result.stderr[-2000:]}")


async def _write_all(repo: PgSkillSchemaRepository, tenant: str, skill_id: str) -> None:
    """Write exactly one row into each of the eleven tables for ``tenant``."""
    version = "1.0.0"
    await repo.save_step(
        tenant, SkillStep(skill_id=skill_id, version=version, step_index=0, text="拉取数据")
    )
    await repo.save_tool(
        tenant, SkillTool(skill_id=skill_id, version=version, kind="tool", ref="data.query")
    )
    await repo.save_policy(
        tenant,
        SkillPolicy(skill_id=skill_id, version=version, kind="constraint", text="不得泄露隐私"),
    )
    await repo.save_evaluation(
        tenant,
        SkillEvaluationEntry(skill_id=skill_id, version=version),
    )
    await repo.save_test_case(
        tenant,
        SkillTestCase(
            skill_id=skill_id, version=version, ordinal=0, name="冒烟",
            case_input={"intent": "分析"}, expected=None,
        ),
    )
    await repo.save_test_run(
        tenant,
        SkillTestRun(
            skill_id=skill_id, version=version, status="pass", passed=True,
            duration_ms=None, score=None,
        ),
    )
    await repo.save_experience(
        tenant,
        SkillExperience(
            skill_id=skill_id, version=version, experience_id=str(uuid.uuid4()),
            relation="source", similarity=None,
        ),
    )
    await repo.save_feedback(
        tenant,
        SkillFeedback(
            skill_id=skill_id, version=version, rating=None, signal=None,
            comment="看起来不错", actor_id="u-1",
        ),
    )
    await repo.save_permission(
        tenant,
        SkillPermission(
            skill_id=skill_id, version=version, tool="data.query", level="read", granted=True
        ),
    )
    await repo.save_knowledge_ref(
        tenant,
        SkillKnowledgeRef(
            skill_id=skill_id, version=version, ordinal=0,
            file="references/domain.md", summary=None,
        ),
    )
    await repo.save_example(
        tenant, SkillExample(skill_id=skill_id, version=version, ordinal=0, text="示例一")
    )


# --------------------------------------------------------------------------- #
# the read surface for one tenant, per table                                   #
# --------------------------------------------------------------------------- #
async def _read_all(
    repo: PgSkillSchemaRepository, tenant: str, skill_id: str
) -> dict[str, list]:
    """Read every table back for ``tenant`` (same signature as production)."""
    return {
        "skill_steps": await repo.list_steps(tenant, skill_id),
        "skill_tools": await repo.list_tools(tenant, skill_id),
        "skill_policies": await repo.list_policies(tenant, skill_id),
        "skill_evaluations": await repo.list_evaluations(tenant, skill_id),
        "skill_test_cases": await repo.list_test_cases(tenant, skill_id),
        "skill_test_runs": await repo.list_test_runs(tenant, skill_id),
        "skill_experiences": await repo.list_experiences(tenant, skill_id),
        "skill_feedback": await repo.list_feedback(tenant, skill_id),
        "skill_permissions": await repo.list_permissions(tenant, skill_id),
        "skill_knowledge_refs": await repo.list_knowledge_refs(tenant, skill_id),
        "skill_examples": await repo.list_examples(tenant, skill_id),
    }


async def _purge(pool: asyncpg.Pool, *tenants: str) -> None:
    async with pool.acquire() as conn:
        for table in SKILL_SCHEMA_TABLES:
            await conn.execute(
                f"DELETE FROM {table} WHERE tenant_id = ANY($1::text[])", list(tenants)
            )


# --------------------------------------------------------------------------- #
# DB / migration evidence                                                      #
# --------------------------------------------------------------------------- #
async def test_023_upgrade_twice_is_idempotent_and_tables_exist(pool):
    """``upgrade head`` twice is a no-op the second time; eleven tables exist."""
    _upgrade_head()
    _upgrade_head()  # second run must be a clean no-op

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = ANY($1::text[])",
            list(_EXPECTED_TABLES),
        )
    found = {r["table_name"] for r in rows}
    assert found == set(_EXPECTED_TABLES), f"missing tables: {set(_EXPECTED_TABLES) - found}"

    # skill_evaluations was *extended* additively (010 table + 023 columns).
    async with pool.acquire() as conn:
        cols = await conn.fetch(
            "SELECT column_name, is_nullable, data_type FROM information_schema.columns "
            "WHERE table_name = 'skill_evaluations' AND column_name = ANY($1::text[])",
            ["skill_id", "version", "segment_ref", "segment_note"],
        )
    added = {c["column_name"] for c in cols}
    assert added == {"skill_id", "version", "segment_ref", "segment_note"}
    for c in cols:  # additive ⇒ all nullable, no backfill, no default
        assert c["is_nullable"] == "YES", c["column_name"]


async def test_all_eleven_tables_reject_fabricated_zero_columns(pool):
    """The four *measured* numeric columns are nullable with **no** default.

    This is the schema-level guarantee behind red line 4: an unmeasured number
    can only ever be ``NULL`` — the table cannot silently fill it with ``0``.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT table_name, column_name, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_name = ANY($1::text[]) "
            "AND column_name = ANY($2::text[])",
            ["skill_test_runs", "skill_experiences", "skill_feedback"],
            ["duration_ms", "score", "similarity", "rating"],
        )
    by_key = {(r["table_name"], r["column_name"]): r for r in rows}
    for table, column in (
        ("skill_test_runs", "duration_ms"),
        ("skill_test_runs", "score"),
        ("skill_experiences", "similarity"),
        ("skill_feedback", "rating"),
    ):
        cell = by_key[(table, column)]
        assert cell["is_nullable"] == "YES", f"{table}.{column} must be nullable"
        assert cell["column_default"] is None, f"{table}.{column} must have no default"


# --------------------------------------------------------------------------- #
# positive probe — write + read back, per tenant, for all eleven tables         #
# --------------------------------------------------------------------------- #
async def test_eleven_tables_write_and_read_back_tenant_scoped(pool):
    _upgrade_head()
    repo = PgSkillSchemaRepository(pool=pool)
    skill_a = f"skill-{uuid.uuid4().hex[:8]}"
    skill_b = f"skill-{uuid.uuid4().hex[:8]}"
    try:
        await _write_all(repo, _TENANT_A, skill_a)
        await _write_all(repo, _TENANT_B, skill_b)

        view_a = await _read_all(repo, _TENANT_A, skill_a)
        # Every table returned exactly the owner's single row.
        for table, rows in view_a.items():
            assert len(rows) == 1, f"{table}: expected 1 row for owner, got {len(rows)}"
            assert rows[0].skill_id == skill_a, table

        view_b = await _read_all(repo, _TENANT_B, skill_b)
        for table, rows in view_b.items():
            assert len(rows) == 1, f"{table}: expected 1 row for owner, got {len(rows)}"
            assert rows[0].skill_id == skill_b, table

        # Round-trip integrity on a couple of representative rows.
        assert view_a["skill_steps"][0].text == "拉取数据"
        assert view_a["skill_knowledge_refs"][0].file == "references/domain.md"
        assert view_a["skill_examples"][0].text == "示例一"
        assert view_a["skill_evaluations"][0].target_id  # NOT NULL uuid supplied
    finally:
        await _purge(pool, _TENANT_A, _TENANT_B)


# --------------------------------------------------------------------------- #
# negative probe 1 — cross-tenant reads are empty (all eleven tables)           #
# --------------------------------------------------------------------------- #
async def test_cross_tenant_reads_are_empty_for_all_tables(pool):
    _upgrade_head()
    repo = PgSkillSchemaRepository(pool=pool)
    skill_a = f"skill-{uuid.uuid4().hex[:8]}"
    try:
        await _write_all(repo, _TENANT_A, skill_a)

        # Tenant B never sees A's rows — for any table, even with the same id.
        view_b = await _read_all(repo, _TENANT_B, skill_a)
        for table, rows in view_b.items():
            assert rows == [], f"cross-tenant leak in {table}: {rows!r}"

        # And the owner *does* still see them (the case is not vacuous).
        view_a = await _read_all(repo, _TENANT_A, skill_a)
        for table, rows in view_a.items():
            assert len(rows) == 1, f"owner cannot read {table}"
    finally:
        await _purge(pool, _TENANT_A, _TENANT_B)


# --------------------------------------------------------------------------- #
# negative probe 2 — unmeasured numbers read back as NULL, never 0              #
# --------------------------------------------------------------------------- #
async def test_unmeasured_numbers_are_null_not_zero(pool):
    _upgrade_head()
    repo = PgSkillSchemaRepository(pool=pool)
    skill_a = f"skill-{uuid.uuid4().hex[:8]}"
    try:
        await _write_all(repo, _TENANT_A, skill_a)

        run = (await repo.list_test_runs(_TENANT_A, skill_a))[0]
        assert run.duration_ms is None and run.duration_ms != 0
        assert run.score is None and run.score != 0

        exp = (await repo.list_experiences(_TENANT_A, skill_a))[0]
        assert exp.similarity is None and exp.similarity != 0

        fb = (await repo.list_feedback(_TENANT_A, skill_a))[0]
        assert fb.rating is None and fb.rating != 0

        case = (await repo.list_test_cases(_TENANT_A, skill_a))[0]
        assert case.expected is None  # no expectation ⇒ NULL, not {}

        # Cross-check straight in SQL: jsonb NULL and numeric NULL (never 0).
        async with pool.acquire() as conn:
            raw = await conn.fetchrow(
                "SELECT score, duration_ms FROM skill_test_runs WHERE tenant_id=$1",
                _TENANT_A,
            )
        assert raw["score"] is None and raw["duration_ms"] is None
    finally:
        await _purge(pool, _TENANT_A, _TENANT_B)


# --------------------------------------------------------------------------- #
# counterfactual — dropping the tenant predicate makes isolation red            #
# --------------------------------------------------------------------------- #
async def test_counterfactual_dropping_tenant_predicate_breaks_isolation(
    pool, monkeypatch
):
    """Remove the tenant discriminator ⇒ cross-tenant rows appear (isolation red).

    The isolation invariant asserted by
    ``test_cross_tenant_reads_are_empty_for_all_tables`` is
    ``A's row ∉ B's view``. With the predicate dropped here, ``B`` *does* see
    ``A``'s row — i.e. that exact assertion would fail. This proves the tenant
    predicate is load-bearing, so the negative probe above is not vacuous.
    """
    _upgrade_head()
    repo = PgSkillSchemaRepository(pool=pool)
    # Same skill_id for both tenants so only the tenant predicate can separate
    # them — the sharpest possible counterfactual.
    skill = f"skill-{uuid.uuid4().hex[:8]}"
    try:
        await _write_all(repo, _TENANT_A, skill)

        # Baseline: with the real predicate, B sees nothing for this skill_id.
        assert await repo.list_steps(_TENANT_B, skill) == []

        # SABOTAGE: drop the tenant discriminator in one place.
        monkeypatch.setattr(
            PgSkillSchemaRepository,
            "_tenant_where",
            # Always true, and still consumes $1 (cast pins its type so asyncpg
            # can prepare the statement) — i.e. the tenant filter is gone.
            lambda self: "($1::text IS NOT NULL OR TRUE)",
        )
        leaked = await repo.list_steps(_TENANT_B, skill)

        # The isolation invariant is now violated ⇒ the negative probe is red.
        assert any(s.skill_id == skill for s in leaked), (
            "counterfactual did not reproduce the leak — the predicate would be "
            "load-bearing by accident"
        )
    finally:
        monkeypatch.undo()
        await _purge(pool, _TENANT_A, _TENANT_B)
