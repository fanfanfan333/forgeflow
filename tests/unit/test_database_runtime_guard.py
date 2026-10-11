from unittest.mock import AsyncMock, Mock

import pytest

from forgeflow import database


@pytest.mark.parametrize(
    "flags", [None, (True, False, True), (False, True, True), (False, False, False)]
)
def test_runtime_rejects_bypass_roles_or_missing_rls(flags):
    with pytest.raises(RuntimeError, match="forced RLS"):
        database._validate_runtime_flags(flags)


def test_runtime_accepts_restricted_role_and_forced_rls():
    database._validate_runtime_flags((False, False, True))


@pytest.mark.asyncio
async def test_unsafe_sync_connection_closes_async_pool(monkeypatch):
    connection = AsyncMock()
    connection.fetchrow.return_value = (False, False, True)
    checkout = AsyncMock()
    checkout.__aenter__.return_value = connection
    pool = Mock()
    pool.acquire.return_value = checkout
    pool.close = AsyncMock()
    monkeypatch.setattr(database.asyncpg, "create_pool", AsyncMock(return_value=pool))

    def unsafe(dsn):
        database._validate_runtime_flags((True, False, True))

    monkeypatch.setattr(database, "_verify_sync_runtime", unsafe)
    with pytest.raises(RuntimeError, match="non-superuser"):
        await database.init_pool()
    pool.close.assert_awaited_once()
