"""INC46 T34 (pg-档) — rollout / metrics / rollback tables on real PostgreSQL.

Proves, against the live dev PostgreSQL, what the in-memory store cannot:

* migration ``032`` really created ``skill_rollouts`` / ``rollout_metrics`` /
  ``skill_rollbacks`` with a ``NOT NULL`` ``tenant_id`` (红线 5), the ``state``
  and ``stage_pct`` vocabulary ``CHECK``s and ``ON DELETE RESTRICT`` foreign
  keys — enforced by the database, not merely by Python;
* a rollout, its per-stage metrics and its rollback rows round-trip through a
  **fresh** store instance (so the values really live in the tables);
* unmeasured metrics stay ``NULL`` (红线 4 —— never a default ``0``);
* the rollback ledger is **append-only** (红线 6 / 20): two rollbacks ⇒ two
  rows, neither the candidate version nor any history row is deleted/rewritten;
* the whole thing is tenant-scoped (红线 5): a second tenant reads nothing.

It dials the same dev database the rest of the postgres-profile suite uses and
skips cleanly when it is unreachable (mirroring ``test_inc46_version_chain_pg.py``).
"""

from __future__ import annotations

import socket
import uuid

import pytest

from forgeflow.config import get_settings
from forgeflow.rollout.store import (
    STATE_CANARY,
    MetricsRecord,
    PostgresRolloutStore,
    RollbackRecord,
    RolloutRecord,
)

_TENANT_A = f"t-t34-pg-a-{uuid.uuid4().hex[:8]}"
_TENANT_B = f"t-t34-pg-b-{uuid.uuid4().hex[:8]}"
_SKILL = "sk-t34-pg"


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
                # Children first: the FKs are ON DELETE RESTRICT.
                await conn.execute(
                    "DELETE FROM skill_rollbacks WHERE tenant_id = ANY($1::text[])",
                    [_TENANT_A, _TENANT_B],
                )
                await conn.execute(
                    "DELETE FROM rollout_metrics WHERE tenant_id = ANY($1::text[])",
                    [_TENANT_A, _TENANT_B],
                )
                await conn.execute(
                    "DELETE FROM skill_rollouts WHERE tenant_id = ANY($1::text[])",
                    [_TENANT_A, _TENANT_B],
                )
        except Exception:  # noqa: BLE001 — cleanup must never mask the test result
            pass
        await created.close()


async def test_schema_has_three_tables_and_constraints(pool) -> None:
    """阳性 — 迁移 032 的三表 / NOT NULL tenant / CHECK / FK 确实存在。"""
    async with pool.acquire() as conn:
        for table in ("skill_rollouts", "rollout_metrics", "skill_rollbacks"):
            assert (
                await conn.fetchval(f"SELECT to_regclass('public.{table}')") is not None
            ), table
            tenant_nullable = await conn.fetchval(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = $1 AND column_name = 'tenant_id'",
                table,
            )
            assert tenant_nullable == "NO", table  # 红线 5
        # state + stage_pct 词表（两个 CHECK）；两张子表的 FK RESTRICT。
        for table, min_checks, min_fks in (
            ("skill_rollouts", 2, 0),
            ("rollout_metrics", 0, 1),
            ("skill_rollbacks", 0, 1),
        ):
            checks = await conn.fetchval(
                "SELECT COUNT(*) FROM pg_constraint "
                "WHERE conrelid = $1::regclass AND contype = 'c'",
                table,
            )
            fks = await conn.fetchval(
                "SELECT COUNT(*) FROM pg_constraint "
                "WHERE conrelid = $1::regclass AND contype = 'f'",
                table,
            )
            assert int(checks) >= min_checks, table
            assert int(fks) >= min_fks, table


async def test_rollout_metrics_and_rollbacks_roundtrip(pool) -> None:
    """阳性 — 灰度 + 指标 + 回滚真落库、可被新实例读回；他租户读空。"""
    store = PostgresRolloutStore(_sync_dsn())
    rollout = store.save_rollout(
        RolloutRecord(
            tenant_id=_TENANT_A,
            skill_id=_SKILL,
            candidate_version="1.1.0",
            incumbent_version="1.0.0",
            stage_pct=5,
            state=STATE_CANARY,
        )
    )
    # 一个阶段窗口：成功 25/30；窗口 25h；延迟未测量 ⇒ NULL（红线 4）。
    # 每个浮点列用**互不相同**的哨兵值，顺带钉死列序（列错位会立刻被断言抓到）。
    store.add_metrics(
        MetricsRecord(
            tenant_id=_TENANT_A,
            rollout_id=rollout.id,
            stage_pct=5,
            labeled_runs=30,
            successes=25,
            success_rate=0.8333,
            success_rate_lb=0.6555,
            validate_fail_rate=0.0333,
            rework_rate=0.0667,
            p95_latency_ms=None,  # 未测量 ⇒ NULL
            cost=1.25,
            window_hours=25.0,
        )
    )
    # 指标窗口期间出现 DANGEROUS ⇒ 自动回滚（只追加）。
    store.add_rollback(
        RollbackRecord(
            tenant_id=_TENANT_A,
            rollout_id=rollout.id,
            from_version="1.1.0",
            to_version="1.0.0",
            trigger="dangerous_escalation",
            reason="DANGEROUS 越权 —— 立即回滚",
        )
    )
    # 指针切回 incumbent（只改这一行的状态与暴露，不删候选）。
    rollout.state = "rolled_back"
    rollout.stage_pct = 0
    store.save_rollout(rollout)

    fresh = PostgresRolloutStore(_sync_dsn())
    back = fresh.get_rollout(_TENANT_A, rollout.id)
    assert back is not None
    assert back.candidate_version == "1.1.0" and back.incumbent_version == "1.0.0"
    assert back.state == "rolled_back" and back.stage_pct == 0
    metrics = fresh.list_metrics(_TENANT_A, rollout.id)
    assert len(metrics) == 1 and metrics[0].labeled_runs == 30
    m = metrics[0]
    assert m.success_rate == pytest.approx(0.8333)
    assert m.success_rate_lb == pytest.approx(0.6555)
    assert m.validate_fail_rate == pytest.approx(0.0333)
    assert m.rework_rate == pytest.approx(0.0667)
    assert m.cost == pytest.approx(1.25)
    assert m.window_hours == pytest.approx(25.0)
    assert m.p95_latency_ms is None  # 未测量 ⇒ NULL（红线 4）
    rollbacks = fresh.list_rollbacks(_TENANT_A, rollout.id)
    assert len(rollbacks) == 1 and rollbacks[0].trigger == "dangerous_escalation"

    # Tenant fail-closed: another tenant sees nothing.
    assert fresh.get_rollout(_TENANT_B, rollout.id) is None
    assert fresh.list_metrics(_TENANT_B, rollout.id) == []
    assert fresh.list_rollbacks(_TENANT_B, rollout.id) == []
    assert fresh.list_rollouts(_TENANT_B, _SKILL) == []


async def test_rollback_ledger_is_append_only(pool) -> None:
    """阳性 — 两次回滚 ⇒ 两行（追加而非覆盖）；候选版本仍在记录里。"""
    store = PostgresRolloutStore(_sync_dsn())
    rollout = store.save_rollout(
        RolloutRecord(
            tenant_id=_TENANT_A,
            skill_id=_SKILL,
            candidate_version="1.2.0",
            incumbent_version="1.0.0",
            stage_pct=25,
        )
    )
    for trigger in ("success_rate_drop", "manual"):
        store.add_rollback(
            RollbackRecord(
                tenant_id=_TENANT_A,
                rollout_id=rollout.id,
                from_version="1.2.0",
                to_version="1.0.0",
                trigger=trigger,
                reason=trigger,
            )
        )
    fresh = PostgresRolloutStore(_sync_dsn())
    rows = fresh.list_rollbacks(_TENANT_A, rollout.id)
    assert len(rows) == 2  # 追加而非覆盖（红线 6 / 20）
    assert {r.trigger for r in rows} == {"success_rate_drop", "manual"}
    assert fresh.get_rollout(_TENANT_A, rollout.id).candidate_version == "1.2.0"


async def test_database_rejects_bad_stage_and_unknown_state(pool) -> None:
    """阴性 — ``stage_pct`` / ``state`` 的 CHECK 在**数据库层**拒绝非法取值。"""
    asyncpg = pytest.importorskip("asyncpg")
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(
                "INSERT INTO skill_rollouts "
                "(tenant_id, skill_id, candidate_version, incumbent_version, stage_pct, state) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                _TENANT_A, _SKILL, "1.1.0", "1.0.0", 7, "canary",  # 7 ∉ {0,5,25,100}
            )
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(
                "INSERT INTO skill_rollouts "
                "(tenant_id, skill_id, candidate_version, incumbent_version, stage_pct, state) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                _TENANT_A, _SKILL, "1.1.0", "1.0.0", 5, "bogus",  # 非法状态
            )


async def test_null_tenant_is_rejected_by_the_database(pool) -> None:
    """阴性 — ``tenant_id`` 的 NOT NULL 在数据库层抵御无租户写入（红线 5）。"""
    asyncpg = pytest.importorskip("asyncpg")
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(
                "INSERT INTO skill_rollouts "
                "(tenant_id, skill_id, candidate_version, incumbent_version, stage_pct) "
                "VALUES ($1, $2, $3, $4, $5)",
                None, _SKILL, "1.1.0", "1.0.0", 5,
            )


async def test_fk_restrict_refuses_orphan_child(pool) -> None:
    """阴性 — 指向不存在灰度行的回滚记录被 FK RESTRICT 拒绝（引用完整性）。"""
    asyncpg = pytest.importorskip("asyncpg")
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(
                "INSERT INTO skill_rollbacks "
                "(tenant_id, rollout_id, from_version, to_version, trigger) "
                "VALUES ($1, $2::uuid, $3, $4, $5)",
                _TENANT_A, str(uuid.uuid4()), "1.1.0", "1.0.0", "manual",
            )
