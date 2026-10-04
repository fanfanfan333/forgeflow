"""INC46 T25 (pg-档) — ``artifact_version_edges`` on real PostgreSQL.

Proves, against the live dev PostgreSQL, what the in-memory store cannot:

* migration ``031`` really created ``artifact_version_edges`` with a ``NOT NULL``
  ``tenant_id``, the ``UNIQUE (tenant_id, artifact_id, child_version)`` upsert key
  and the ``edge_kind`` ``CHECK`` (an unknown kind is rejected by the database,
  not merely by Python);
* ``initial`` / ``refine`` / ``revert`` edges round-trip through a **fresh** store
  instance (so the values really live in the table, not in a dict);
* the whole thing is tenant-scoped (红线 5): a second tenant reads the empty set.

It dials the same dev database the rest of the postgres-profile suite uses and
skips cleanly when it is unreachable (mirroring ``test_inc46_quarantine_pg.py``).
"""

from __future__ import annotations

import socket
import uuid

import pytest

from forgeflow.config import get_settings
from forgeflow.documents import version_chain as vc

_TENANT_A = f"t-t25-pg-a-{uuid.uuid4().hex[:8]}"
_TENANT_B = f"t-t25-pg-b-{uuid.uuid4().hex[:8]}"
_ART = "art-t25-pg"


def _sync_dsn() -> str:
    return get_settings().postgres_sync_url.replace("postgresql+psycopg://", "postgresql://")


def _async_dsn() -> str:
    return get_settings().postgres_url.replace("postgresql+asyncpg://", "postgresql://")


def _pg_reachable() -> bool:
    dsn = _sync_dsn()
    host, _, rest = dsn.rpartition("@")[2].partition(":")
    port = int(rest.split("/")[0]) if rest.split("/")[0].isdigit() else 5432
    try:
        with socket.create_connection((host or "127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_reachable(), reason="dev PostgreSQL (5433) unreachable"
)


@pytest.fixture()
async def pool():
    asyncpg = pytest.importorskip("asyncpg")
    from forgeflow.database import _init_connection

    created = await asyncpg.create_pool(
        _async_dsn(), min_size=1, max_size=2, command_timeout=15, init=_init_connection
    )
    # Pin the process-wide chain store to the real table for this test.
    vc.set_version_chain_store(vc.PostgresVersionChainStore(_sync_dsn()))
    try:
        yield created
    finally:
        vc.reset_version_chain_store()
        try:
            async with created.acquire() as conn:
                await conn.execute(
                    "DELETE FROM artifact_version_edges WHERE tenant_id = ANY($1::text[])",
                    [_TENANT_A, _TENANT_B],
                )
        except Exception:  # noqa: BLE001 — cleanup must never mask the test result
            pass
        await created.close()


async def test_schema_has_edges_table_and_constraints(pool) -> None:
    """阳性 — 迁移 031 的表 / NOT NULL tenant / UNIQUE / CHECK 确实存在。"""
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT to_regclass('public.artifact_version_edges')")
            is not None
        )
        tenant_nullable = await conn.fetchval(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'artifact_version_edges' AND column_name = 'tenant_id'"
        )
        assert tenant_nullable == "NO"  # 红线 5
        uniq = await conn.fetchval(
            "SELECT COUNT(*) FROM pg_constraint "
            "WHERE conrelid = 'artifact_version_edges'::regclass AND contype = 'u'"
        )
        assert int(uniq) >= 1  # the (tenant, artifact, child) upsert key
        checks = await conn.fetchval(
            "SELECT COUNT(*) FROM pg_constraint "
            "WHERE conrelid = 'artifact_version_edges'::regclass AND contype = 'c'"
        )
        assert int(checks) >= 1  # the edge_kind vocabulary


async def test_edges_roundtrip_and_tenant_scope(pool) -> None:
    """阳性/阴性 — 三条边真落库、可被新实例读回；他租户读空。"""
    store = vc.PostgresVersionChainStore(_sync_dsn())
    store.record_edge(
        _TENANT_A, artifact_id=_ART, child_version=1, edge_kind=vc.EDGE_INITIAL
    )
    store.record_edge(
        _TENANT_A, artifact_id=_ART, child_version=2, edge_kind=vc.EDGE_REFINE,
        parent_version=1,
    )
    store.record_edge(
        _TENANT_A, artifact_id=_ART, child_version=3, edge_kind=vc.EDGE_REVERT,
        parent_version=2, source_version=1,
    )

    fresh = vc.PostgresVersionChainStore(_sync_dsn())
    edges = {e.child_version: e for e in fresh.list_edges(_TENANT_A, _ART)}
    assert set(edges) == {1, 2, 3}
    assert edges[1].edge_kind == vc.EDGE_INITIAL and edges[1].parent_version is None
    assert edges[2].edge_kind == vc.EDGE_REFINE and edges[2].parent_version == 1
    assert edges[3].edge_kind == vc.EDGE_REVERT and edges[3].source_version == 1
    assert edges[3].parent_version == 2

    # Tenant fail-closed: another tenant sees nothing.
    assert fresh.list_edges(_TENANT_B, _ART) == []
    assert fresh.get_edge(_TENANT_B, _ART, 1) is None


async def test_idempotent_per_child(pool) -> None:
    """阳性 — 同一 child 重复登记不产生第二行（UNIQUE upsert）。"""
    store = vc.PostgresVersionChainStore(_sync_dsn())
    store.record_edge(_TENANT_A, artifact_id=_ART, child_version=7, edge_kind=vc.EDGE_INITIAL)
    store.record_edge(
        _TENANT_A, artifact_id=_ART, child_version=7, edge_kind=vc.EDGE_REFINE, parent_version=3
    )
    rows = store.list_edges(_TENANT_A, _ART)
    assert len([e for e in rows if e.child_version == 7]) == 1


async def test_unknown_edge_kind_is_rejected_by_the_database(pool) -> None:
    """阴性 — ``edge_kind`` 的 CHECK 在**数据库层**拒绝未知取值（不只是 Python 守卫）。"""
    asyncpg = pytest.importorskip("asyncpg")
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(
                "INSERT INTO artifact_version_edges "
                "(tenant_id, artifact_id, child_version, edge_kind) VALUES ($1, $2, $3, $4)",
                _TENANT_A, _ART, 99, "bogus",
            )


async def test_null_tenant_is_rejected_by_the_database(pool) -> None:
    """阴性 — ``tenant_id`` 的 NOT NULL 在数据库层抵御无租户写入（红线 5）。"""
    asyncpg = pytest.importorskip("asyncpg")
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(
                "INSERT INTO artifact_version_edges "
                "(tenant_id, artifact_id, child_version, edge_kind) VALUES ($1, $2, $3, $4)",
                None, _ART, 98, vc.EDGE_INITIAL,
            )
