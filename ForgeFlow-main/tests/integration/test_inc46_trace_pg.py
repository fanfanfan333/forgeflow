"""INC46 T01 (pg-档) — a real run materialises honest ``run_steps`` rows.

Proves, against the **live dev PostgreSQL**, what a fake sink cannot:

  * ``ToolExecutor.execute`` really writes one ``run_steps`` row per tool call
    through the shared application pool (JSONB round-trips as dicts);
  * an **unmeasured** latency is stored as ``NULL`` — never a fabricated ``0``
    (the reason migration ``019`` drops the old ``NOT NULL DEFAULT 0``);
  * the ``019`` upgrade is idempotent (``alembic upgrade head`` twice) and the
    five new columns are all nullable with no default (no backfill);
  * the eight pre-existing ``run_steps`` columns keep their exact type and
    nullability (既有表零变化).

It dials the same dev database the rest of the postgres-profile suite uses
(``localhost:5433``) and skips cleanly when it is unreachable, mirroring
``tests/integration/test_inc44_skill_publish_pg.py``.
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
from forgeflow.runtime import trace_store
from forgeflow.runtime.tool_executor import ToolCallContext, ToolExecutor
from forgeflow.runtime.trace_store import list_for_run, reset_trace_sink

# Captured at import (before conftest's autouse DNS stub) so we reach a real
# 127.0.0.1 rather than the stubbed public IP.
_REAL_GETADDRINFO = socket.getaddrinfo
_DSN = get_settings().postgres_sync_url.replace("postgresql+psycopg://", "postgresql://")
_ROOT = pathlib.Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _postgres_profile(monkeypatch):
    """Exercise the real postgres write path (the trace gate reads this)."""
    monkeypatch.setattr(get_settings(), "storage_backend", "postgres")
    reset_trace_sink()
    yield
    reset_trace_sink()


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
    # The application pool the executor used is separate; drop it too.
    from forgeflow.database import close_pool

    await close_pool()


def _upgrade_head() -> None:
    """Run ``alembic upgrade head`` in a subprocess (brings 019 into the DB)."""
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


def _ctx(*, run_id: str, step_index: int, tenant_id: str, args: dict) -> ToolCallContext:
    return ToolCallContext(
        run_id=run_id,
        step_id=f"{run_id}:0:{step_index}",
        tenant_id=tenant_id,
        user_id="u-inc46",
        role="admin",
        intent="分析文档",
        attempt=0,
        args=args,
    )


async def test_real_run_materialises_honest_run_steps(pool):
    _upgrade_head()
    tenant = f"t-inc46pg-{uuid.uuid4().hex[:8]}"
    run_id = str(uuid.uuid4())
    try:
        # --- a real, measured step ⇒ a row with a real (non-null) latency ------
        ok = await ToolExecutor().execute(
            "docs.parse",
            ctx=_ctx(run_id=run_id, step_index=0, tenant_id=tenant, args={"text": "甲\n\n乙"}),
        )
        assert ok.status == "ok"
        assert ok.latency_ms is not None and ok.latency_ms > 0

        # --- a blocked step ⇒ a row whose latency is NULL (never 0) -----------
        blocked = await ToolExecutor().execute(
            "docs.parse",
            ctx=_ctx(run_id=run_id, step_index=1, tenant_id=tenant, args={}),
            blocked_reason="缺少文本输入",
        )
        assert blocked.status == "blocked"
        assert blocked.latency_ms is None

        # Read back through the store (tenant-scoped, ascending step_index).
        steps = await list_for_run(tenant, run_id)
        assert len(steps) == 2
        assert [s.step_index for s in steps] == [0, 1]
        first, second = steps
        assert first.status == "ok"
        assert first.tool == "docs.parse"
        assert first.latency_ms is not None and first.latency_ms > 0
        assert first.input["args"] == {"text": "甲\n\n乙"}
        assert first.input["arguments_hash"] == ok.arguments_hash
        # ``output`` carries the whole invocation verbatim.
        assert first.output["tool"] == "docs.parse"
        assert first.output["status"] == "ok"
        assert first.actor_user_id == "u-inc46"
        assert first.attempt == 0

        assert second.status == "blocked"
        assert second.latency_ms is None

        # Raw SQL cross-check: NULL, not 0 (the honesty red line).
        async with pool.acquire() as conn:
            raw = await conn.fetchrow(
                "SELECT latency_ms, status FROM run_steps "
                "WHERE run_id=$1::uuid AND step_index=1",
                run_id,
            )
        assert raw is not None
        assert raw["latency_ms"] is None
        assert raw["status"] == "blocked"

        # A tenant that does not own the run sees nothing (fail-closed).
        assert await list_for_run("t-inc46-someone-else", run_id) == []
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM run_steps WHERE tenant_id=$1", tenant)


async def test_019_upgrade_head_is_idempotent_and_schema_is_honest(pool):
    _upgrade_head()
    _upgrade_head()  # second run must be a clean no-op

    async with pool.acquire() as conn:
        cols = await conn.fetch(
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_name='run_steps' "
            "AND column_name = ANY($1)",
            ["latency_ms", "status", "artifact_ref", "verification", "actor_user_id", "attempt"],
        )
    by_name = {c["column_name"]: c for c in cols}

    # latency_ms: nullable, no default (019 dropped NOT NULL + DEFAULT 0).
    assert by_name["latency_ms"]["is_nullable"] == "YES"
    assert by_name["latency_ms"]["column_default"] is None

    # The five new columns exist, are all nullable, with no backfill default.
    for name in ("status", "artifact_ref", "verification", "actor_user_id", "attempt"):
        assert name in by_name, f"019 must add run_steps.{name}"
        assert by_name[name]["is_nullable"] == "YES", name
        assert by_name[name]["column_default"] is None, name


async def test_existing_run_steps_columns_unchanged(pool):
    """The eight pre-019 columns keep their exact type + nullability."""
    _upgrade_head()
    async with pool.acquire() as conn:
        cols = await conn.fetch(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns WHERE table_name='run_steps'"
        )
    got = {c["column_name"]: (c["data_type"], c["is_nullable"]) for c in cols}
    expected = {
        "id": ("uuid", "NO"),
        "run_id": ("uuid", "NO"),
        "tenant_id": ("text", "YES"),
        "step_index": ("integer", "NO"),
        "step_type": ("character varying", "NO"),
        "tool": ("character varying", "YES"),
        "input": ("jsonb", "NO"),
        "output": ("jsonb", "NO"),
        "created_at": ("timestamp with time zone", "NO"),
    }
    for name, shape in expected.items():
        assert got.get(name) == shape, f"{name}: {got.get(name)} != {shape}"
    # And the trace gate is honestly ON for this profile.
    assert trace_store.trace_persistence_enabled() is True
