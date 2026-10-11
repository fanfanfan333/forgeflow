"""asyncpg connection pool factory — shared across all modules."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from forgeflow.config import get_settings
from forgeflow.tenant_scope import current_tenant

_pool: TenantPool | None = None

_RUNTIME_GUARD_SQL = """
SELECT rolsuper, rolbypassrls,
       EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
              WHERE n.nspname='public' AND c.relname='workspace_runs'
                AND c.relrowsecurity AND c.relforcerowsecurity)
FROM pg_roles WHERE rolname=current_user
"""


def _validate_runtime_flags(flags: Any) -> None:
    if flags is None or tuple(flags) != (False, False, True):
        raise RuntimeError(
            "Runtime database requires a non-superuser, NOBYPASSRLS role and forced RLS migrations"
        )


def _verify_sync_runtime(dsn: str) -> None:
    import psycopg

    with psycopg.connect(
        dsn.replace("postgresql+psycopg://", "postgresql://"), connect_timeout=5
    ) as conn:
        _validate_runtime_flags(conn.execute(_RUNTIME_GUARD_SQL).fetchone())


@asynccontextmanager
async def tenant_connection(
    pool: Any, tenant_id: str | None = None
) -> AsyncGenerator[asyncpg.Connection, None]:
    """Every checkout is a transaction; SET LOCAL never leaks through a pool.

    An absent scope is empty, which RLS denies. Test doubles need not implement
    PostgreSQL transactions; real asyncpg connections always do.
    """
    raw = pool.raw if isinstance(pool, TenantPool) else pool
    async with raw.acquire() as conn:
        if isinstance(conn, (asyncpg.Connection, asyncpg.pool.PoolConnectionProxy)):
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.tenant_id', $1, true)",
                    tenant_id or current_tenant() or "",
                )
                scope = tenant_id or current_tenant()
                if scope:
                    await conn.execute(
                        "INSERT INTO tenant_registry(tenant_id) VALUES($1) ON CONFLICT DO NOTHING",
                        scope,
                    )
                yield conn
        else:
            yield conn


class TenantPool:
    """Pool facade that scopes legacy inline SQL as well as repositories."""

    def __init__(self, raw: asyncpg.Pool) -> None:
        self.raw = raw

    def acquire(self):
        return tenant_connection(self)

    async def close(self) -> None:
        await self.raw.close()


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Per-connection setup. Registers a JSON/JSONB codec so dict bindings
    flow directly into JSONB columns and reads come back as dicts — without
    this, every write to audit_log.metadata (and similar) silently fails
    with 'expected str, got dict'."""
    await conn.set_type_codec("json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def init_pool() -> TenantPool:
    global _pool
    settings = get_settings()
    # Strip the +asyncpg prefix that asyncpg doesn't understand
    dsn = settings.postgres_url.replace("postgresql+asyncpg://", "postgresql://")
    raw_pool = await asyncpg.create_pool(
        dsn,
        min_size=3,
        max_size=20,
        command_timeout=60,
        statement_cache_size=0,  # required for pgBouncer compatibility
        init=_init_connection,
    )
    try:
        async with raw_pool.acquire() as conn:
            _validate_runtime_flags(await conn.fetchrow(_RUNTIME_GUARD_SQL))
        await asyncio.to_thread(_verify_sync_runtime, settings.postgres_sync_url)
    except BaseException:
        await raw_pool.close()
        raise
    _pool = TenantPool(raw_pool)
    return _pool


async def get_pool() -> TenantPool:
    if _pool is None:
        await init_pool()
    assert _pool is not None
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def get_connection() -> AsyncGenerator[asyncpg.Connection, None]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn
