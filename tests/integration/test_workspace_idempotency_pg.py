"""Atomic admission against a disposable PostgreSQL database."""

import asyncio
import os

import asyncpg
import pytest

from forgeflow.workspace.models import WorkspaceRunRecord
from forgeflow.workspace.store import PgWorkspaceStore


@pytest.mark.skipif(not os.getenv("IDEMPOTENCY_TEST_PG_DSN"), reason="disposable PostgreSQL not configured")
async def test_concurrent_claims_persist_only_one_run():
    pool = await asyncpg.create_pool(os.environ["IDEMPOTENCY_TEST_PG_DSN"], min_size=1, max_size=8)
    try:
        async with pool.acquire() as conn:
            await conn.execute("CREATE TABLE tenant_registry (tenant_id text primary key)")
            await conn.execute("""CREATE TABLE workspace_runs (
                run_id text primary key, tenant_id text not null, session_id text not null,
                parent_run_id text, actor_user_id text not null, actor_role text not null,
                intent text not null, title text not null, workflow_type text not null,
                status text not null, created_at text not null, updated_at text not null, request_digest varchar(64)
            )""")
        store = PgWorkspaceStore()
        store._get_pool = lambda: asyncio.sleep(0, result=pool)
        record = WorkspaceRunRecord(run_id="run-1", tenant_id="tenant-a", session_id="run-1", actor_user_id="alice")
        results = await asyncio.gather(*(store.claim(record) for _ in range(12)))
        assert sum(results) == 1
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM workspace_runs") == 1
    finally:
        await pool.close()
