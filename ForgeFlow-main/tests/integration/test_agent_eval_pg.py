"""INC8 Phase-B / T05 — real PostgreSQL integration for the eval-sample layer.

Runs against the live dev Postgres (docker-compose on :5433 by default) and skips
cleanly when one is not reachable — the same discipline as
``tests/integration/test_auth_db.py``. It proves the parts a mocked pool cannot:

  * migrations up to ``head`` are applied by a real ``alembic upgrade head`` and
    running ``head`` twice is idempotent (version stays at the current head,
    ``013`` since INC12-A2);
  * ``PgEvalSampleRepository`` writes → reads back through real ``asyncpg``;
  * the tenant id is an opaque string: the non-UUID ``"default"`` sentinel is
    stored as the literal ``"default"`` (migration ``013`` made the column
    ``TEXT``), and two distinct slug tenants stay pairwise invisible — the
    regression guard for the old ``"default" → NULL`` UUID-coercion leak;
  * the additive ``context_build_stats.skill_refs`` column round-trips.
"""

from __future__ import annotations

import os
import pathlib
import re
import socket
import subprocess
import sys
import uuid

import asyncpg
import pytest

from forgeflow.config import get_settings
from forgeflow.repositories.eval_sample_repo import EvalSample
from forgeflow.repositories.postgres.eval_sample_repo import PgEvalSampleRepository

# Captured at import (before conftest's autouse DNS stub) so we reach a real
# 127.0.0.1 rather than the stubbed public IP.
_REAL_GETADDRINFO = socket.getaddrinfo
_DSN = get_settings().postgres_sync_url.replace("postgresql+psycopg://", "postgresql://")

_ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture
async def pool(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)
    try:
        p = await asyncpg.create_pool(_DSN, min_size=1, max_size=3, timeout=4)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"live Postgres not reachable ({exc})")
    yield p
    await p.close()


def _upgrade_head(env: dict[str, str] | None = None) -> None:
    """Run ``alembic upgrade head`` in a **subprocess**.

    A subprocess (not in-process ``alembic.command``) is deliberate: alembic's
    ``env.py`` calls ``logging.config.fileConfig(...)``, whose default
    ``disable_existing_loggers=True`` would silence every logger already created
    in this process and break an unrelated later ``caplog`` test. Isolating the
    migration in its own process keeps the test session's logging intact.
    """
    child_env = dict(os.environ)
    child_env["POSTGRES_SYNC_URL"] = get_settings().postgres_sync_url
    if env:
        child_env.update(env)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(_ROOT),
        env=child_env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade head failed:\n{result.stderr[-2000:]}")


# --------------------------------------------------------------------------- #
# migration                                                                    #
# --------------------------------------------------------------------------- #
async def test_migration_head_upgrade_twice_is_idempotent(pool):
    _upgrade_head()
    # Re-entrant: a second upgrade to head must be a no-op, never an error.
    _upgrade_head()
    async with pool.acquire() as conn:
        version = await conn.fetchval("SELECT version_num FROM alembic_version")
        table = await conn.fetchval("SELECT to_regclass('public.agent_eval_samples')")
    # Head moved from 013 to 014 in INC12-A6 (run_id linkage columns → TEXT).
    assert version == "014"
    assert table is not None


def test_012_source_is_reentrant_and_mirrors_downgrade():
    """Every statement is ``IF NOT EXISTS`` — the SQL itself is re-runnable."""
    source = (_ROOT / "alembic" / "versions" / "012_agent_eval_samples.py").read_text(
        encoding="utf-8"
    )
    assert "CREATE TABLE IF NOT EXISTS agent_eval_samples" in source
    assert "ADD COLUMN IF NOT EXISTS skill_refs" in source
    assert re.search(r"CREATE TABLE(?! IF NOT EXISTS)", source) is None
    assert re.search(r"ADD COLUMN(?! IF NOT EXISTS)", source) is None
    downgrade_body = source.split("def downgrade()", 1)[1]
    assert "DROP COLUMN IF EXISTS skill_refs" in downgrade_body
    assert "DROP TABLE IF EXISTS agent_eval_samples" in downgrade_body


# --------------------------------------------------------------------------- #
# repository round-trip                                                        #
# --------------------------------------------------------------------------- #
async def test_sample_write_then_read_back(pool):
    _upgrade_head()
    repo = PgEvalSampleRepository(pool=pool)
    tenant = str(uuid.uuid4())
    run_id = f"hub-{uuid.uuid4().hex[:10]}"
    try:
        await repo.save_sample(
            tenant,
            EvalSample(
                tenant_id=tenant,
                run_id=run_id,
                metric_name="groundedness",
                metric_value=0.83,
                metric_unit="score",
                dimension="quality",
                source="judge",
                dataset="sales-eval-20",
            ),
        )
        rows = await repo.list_samples(tenant, metric="groundedness", limit=10)
        match = [r for r in rows if r.run_id == run_id]
        assert match, "the just-written sample must be readable back"
        assert match[0].metric_value == pytest.approx(0.83)
        assert match[0].dataset == "sales-eval-20"

        agg = await repo.aggregate(tenant, "groundedness")
        assert agg["has_data"] is True
        assert agg["count"] >= 1
        assert agg["avg"] == pytest.approx(0.83)
    finally:
        async with pool.acquire() as conn:
            # ``tenant_id`` is TEXT (migration 013) — clean up by the raw string,
            # not a ``uuid.UUID`` (which asyncpg would reject against a text column).
            await conn.execute(
                "DELETE FROM agent_eval_samples WHERE tenant_id = $1", tenant
            )


async def test_empty_cohort_aggregate_is_no_data(pool):
    _upgrade_head()
    repo = PgEvalSampleRepository(pool=pool)
    agg = await repo.aggregate(str(uuid.uuid4()), "groundedness")
    assert agg == {"count": 0, "avg": None, "min": None, "max": None, "has_data": False}


async def test_default_tenant_is_stored_as_the_literal_string(pool):
    """The non-UUID ``"default"`` tenant is stored verbatim — not coerced to NULL.

    Regression guard for INC12-A2: migration ``013`` made the ``tenant_id``
    column opaque ``TEXT``, so ``save_sample`` writes the literal scope key and
    ``list_samples`` reads it back with the same key. Under the retired UUID
    coercion this row was stored as ``NULL``.
    """
    _upgrade_head()
    repo = PgEvalSampleRepository(pool=pool)
    run_id = f"hub-{uuid.uuid4().hex[:10]}"
    try:
        await repo.save_sample(
            "default",
            EvalSample(
                tenant_id="default",
                run_id=run_id,
                metric_name="hallucination",
                metric_value=0.0,
                dimension="quality",
            ),
        )
        async with pool.acquire() as conn:
            stored = await conn.fetchval(
                "SELECT tenant_id FROM agent_eval_samples WHERE run_id = $1", run_id
            )
        assert stored == "default"  # the literal string, NOT NULL
        # A literal "default" filter matches the very row just written.
        rows = await repo.list_samples("default", metric="hallucination", limit=50)
        assert any(r.run_id == run_id for r in rows)
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM agent_eval_samples WHERE run_id = $1", run_id)


async def test_non_uuid_tenants_stay_pairwise_invisible(pool):
    """Two distinct slug tenants must never see each other's rows.

    The exact cross-tenant leak INC12-A2 fixes: under the retired UUID coercion
    both ``"t-alpha"`` and ``"t-beta"`` collapsed onto ``NULL``, so
    ``list_samples("t-alpha")`` returned ``"t-beta"``'s rows. With the opaque
    ``TEXT`` column each tenant is stored and filtered by its own string.
    """
    _upgrade_head()
    repo = PgEvalSampleRepository(pool=pool)
    alpha_run = f"hub-{uuid.uuid4().hex[:10]}"
    beta_run = f"hub-{uuid.uuid4().hex[:10]}"
    try:
        await repo.save_sample(
            "t-alpha",
            EvalSample(
                tenant_id="t-alpha",
                run_id=alpha_run,
                metric_name="groundedness",
                metric_value=0.9,
                dimension="quality",
            ),
        )
        await repo.save_sample(
            "t-beta",
            EvalSample(
                tenant_id="t-beta",
                run_id=beta_run,
                metric_name="groundedness",
                metric_value=0.1,
                dimension="quality",
            ),
        )
        async with pool.acquire() as conn:
            stored = {
                r["run_id"]: r["tenant_id"]
                for r in await conn.fetch(
                    "SELECT run_id, tenant_id FROM agent_eval_samples "
                    "WHERE run_id = ANY($1)",
                    [alpha_run, beta_run],
                )
            }
        assert stored[alpha_run] == "t-alpha"
        assert stored[beta_run] == "t-beta"

        alpha_rows = await repo.list_samples("t-alpha", metric="groundedness", limit=50)
        assert [r.run_id for r in alpha_rows] == [alpha_run], alpha_rows
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM agent_eval_samples WHERE tenant_id = ANY($1)",
                ["t-alpha", "t-beta"],
            )


# --------------------------------------------------------------------------- #
# additive column                                                              #
# --------------------------------------------------------------------------- #
async def test_context_build_stats_skill_refs_round_trips(pool):
    _upgrade_head()
    row_id = str(uuid.uuid4())
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO context_build_stats
                  (id, tenant_id, run_id, tokens_raw, tokens_used,
                   compression_ratio, hit_rate, skill_refs, created_at)
                VALUES ($1, NULL, NULL, 100, 50, 0.5, 0.5, $2, now())
                """,
                uuid.UUID(row_id),
                ["skill-1", "skill-2"],
            )
            refs = await conn.fetchval(
                "SELECT skill_refs FROM context_build_stats WHERE id = $1",
                uuid.UUID(row_id),
            )
        assert list(refs) == ["skill-1", "skill-2"]
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM context_build_stats WHERE id = $1", uuid.UUID(row_id)
            )
