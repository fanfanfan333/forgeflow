"""INC46 T35 (pg-档) — lifecycle / merge-proposal tables on real PostgreSQL.

Proves, against the live dev PostgreSQL, what the in-memory store cannot:

* migration ``033`` really created ``skill_lifecycle_events`` /
  ``skill_merge_proposals`` with a ``NOT NULL`` ``tenant_id`` (红线 5) and the
  state / status vocabulary ``CHECK``s — enforced by the database, not merely by
  Python;
* lifecycle events and merge proposals round-trip through a **fresh** store
  instance (so the values really live in the tables), including the ``source_skill_ids``
  JSONB provenance chain;
* the lifecycle ledger is **append-only** (红线 6): repeated transitions add rows,
  none of the earlier rows is rewritten;
* the whole thing is tenant-scoped (红线 5): a second tenant reads nothing.

It dials the same dev database the rest of the postgres-profile suite uses and
skips cleanly when it is unreachable (mirroring ``test_inc46_rollout_pg.py``).
"""

from __future__ import annotations

import socket
import uuid

import pytest

from forgeflow.config import get_settings
from forgeflow.lifecycle.store import (
    LifecycleEvent,
    MergeProposalRecord,
    PostgresLifecycleStore,
)

_TENANT_A = f"t-t35-pg-a-{uuid.uuid4().hex[:8]}"
_TENANT_B = f"t-t35-pg-b-{uuid.uuid4().hex[:8]}"
_SKILL = "sk-t35-pg"


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
    try:
        yield created
    finally:
        try:
            async with created.acquire() as conn:
                await conn.execute(
                    "DELETE FROM skill_merge_proposals WHERE tenant_id = ANY($1::text[])",
                    [_TENANT_A, _TENANT_B],
                )
                await conn.execute(
                    "DELETE FROM skill_lifecycle_events WHERE tenant_id = ANY($1::text[])",
                    [_TENANT_A, _TENANT_B],
                )
        except Exception:  # noqa: BLE001 — cleanup must never mask the test result
            pass
        await created.close()


async def test_schema_has_two_tables_and_constraints(pool) -> None:
    """阳性 — 迁移 033 的两表 / NOT NULL tenant / CHECK 确实存在。"""
    async with pool.acquire() as conn:
        for table in ("skill_lifecycle_events", "skill_merge_proposals"):
            assert (
                await conn.fetchval(f"SELECT to_regclass('public.{table}')") is not None
            ), table
            tenant_nullable = await conn.fetchval(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = $1 AND column_name = 'tenant_id'",
                table,
            )
            assert tenant_nullable == "NO", table  # 红线 5
        # skill_lifecycle_events：from_state + to_state 两个 CHECK。
        checks = await conn.fetchval(
            "SELECT COUNT(*) FROM pg_constraint "
            "WHERE conrelid = 'skill_lifecycle_events'::regclass AND contype = 'c'",
        )
        assert int(checks) >= 2
        # skill_merge_proposals：status CHECK。
        checks2 = await conn.fetchval(
            "SELECT COUNT(*) FROM pg_constraint "
            "WHERE conrelid = 'skill_merge_proposals'::regclass AND contype = 'c'",
        )
        assert int(checks2) >= 1


async def test_events_roundtrip_append_only_and_tenant_scoped(pool) -> None:
    """阳性 — 迁移事件真落库、可被新实例读回；只追加；他租户读空。"""
    store = PostgresLifecycleStore(_sync_dsn())
    store.append_event(
        LifecycleEvent(
            tenant_id=_TENANT_A, skill_id=_SKILL, from_state="published",
            to_state="deprecated", reason="闲置 90 天", actor="scheduler",
        )
    )
    store.append_event(
        LifecycleEvent(
            tenant_id=_TENANT_A, skill_id=_SKILL, from_state="deprecated",
            to_state="published", reason="宽限期内恢复", actor="alice",
        )
    )
    fresh = PostgresLifecycleStore(_sync_dsn())
    events = fresh.list_events(_TENANT_A, _SKILL)
    assert len(events) == 2  # 追加而非覆盖（红线 6）
    assert [e.to_state for e in events] == ["deprecated", "published"]
    assert events[0].from_state == "published" and events[0].reason == "闲置 90 天"
    assert fresh.latest_event(_TENANT_A, _SKILL).to_state == "published"
    # Tenant fail-closed: another tenant sees nothing.
    assert fresh.list_events(_TENANT_B, _SKILL) == []
    assert fresh.latest_event(_TENANT_B, _SKILL) is None


async def test_merge_proposals_roundtrip_with_source_chain(pool) -> None:
    """阳性 — 提案 + JSONB 来源链真落库、可被新实例读回；他租户读空。"""
    store = PostgresLifecycleStore(_sync_dsn())
    primary = f"p-{uuid.uuid4().hex[:6]}"
    duplicate = f"d-{uuid.uuid4().hex[:6]}"
    store.save_proposal(
        MergeProposalRecord(
            tenant_id=_TENANT_A,
            primary_skill_id=primary,
            duplicate_skill_id=duplicate,
            source_skill_ids=sorted([primary, duplicate]),
            description_cosine=0.9636,
            tool_jaccard=1.0,
        )
    )
    fresh = PostgresLifecycleStore(_sync_dsn())
    rows = fresh.list_proposals(_TENANT_A)
    assert len(rows) == 1
    rec = rows[0]
    assert rec.status == "proposed"
    assert sorted(rec.source_skill_ids) == sorted([primary, duplicate])
    assert rec.description_cosine == pytest.approx(0.9636)
    assert rec.tool_jaccard == pytest.approx(1.0)
    assert fresh.get_proposal(_TENANT_B, rec.id) is None
    assert fresh.list_proposals(_TENANT_B) == []
    # find_proposal is order-insensitive and真读库。
    assert fresh.find_proposal(_TENANT_A, duplicate, primary) is not None


async def test_proposal_decision_persists(pool) -> None:
    """阳性 — approve 落 ``approved`` + 决策者 / 合并产物，真落库。"""
    store = PostgresLifecycleStore(_sync_dsn())
    rec = MergeProposalRecord(
        tenant_id=_TENANT_A,
        primary_skill_id="p1",
        duplicate_skill_id="d1",
        source_skill_ids=["d1", "p1"],
        description_cosine=0.9,
        tool_jaccard=0.8,
    )
    store.save_proposal(rec)
    rec.status = "approved"
    rec.decided_by = "alice"
    rec.merged_skill_id = "m1"
    store.update_proposal(rec)
    fresh = PostgresLifecycleStore(_sync_dsn())
    back = fresh.get_proposal(_TENANT_A, rec.id)
    assert back.status == "approved"
    assert back.decided_by == "alice" and back.merged_skill_id == "m1"
    assert sorted(back.source_skill_ids) == ["d1", "p1"]


async def test_database_rejects_unknown_state_and_status(pool) -> None:
    """阴性 — 状态 / 提案 status 的 CHECK 在**数据库层**拒绝非法取值。"""
    asyncpg = pytest.importorskip("asyncpg")
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(
                "INSERT INTO skill_lifecycle_events "
                "(tenant_id, skill_id, from_state, to_state) VALUES ($1, $2, $3, $4)",
                _TENANT_A, _SKILL, "published", "banana",  # 非法状态
            )
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(
                "INSERT INTO skill_merge_proposals "
                "(tenant_id, primary_skill_id, duplicate_skill_id, status) "
                "VALUES ($1, $2, $3, $4)",
                _TENANT_A, "p1", "d1", "bogus",  # 非法提案状态
            )


async def test_null_tenant_is_rejected_by_the_database(pool) -> None:
    """阴性 — ``tenant_id`` 的 NOT NULL 在数据库层抵御无租户写入（红线 5）。"""
    asyncpg = pytest.importorskip("asyncpg")
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(
                "INSERT INTO skill_lifecycle_events "
                "(tenant_id, skill_id, from_state, to_state) VALUES ($1, $2, $3, $4)",
                None, _SKILL, "published", "deprecated",
            )
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(
                "INSERT INTO skill_merge_proposals "
                "(tenant_id, primary_skill_id, duplicate_skill_id) VALUES ($1, $2, $3)",
                None, "p1", "d1",
            )
