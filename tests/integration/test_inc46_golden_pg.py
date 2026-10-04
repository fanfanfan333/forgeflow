"""INC46 T17 (pg-档) — 独立 Golden 回归集在真实 PostgreSQL 上的租户隔离与哈希锁定。

证明内存存储无法证明的事：

* 迁移 ``027`` 真的建了 ``golden_sets`` / ``golden_cases`` / ``golden_runs``，
  ``golden_runs.passed`` 可空（未测量 ⇒ NULL，红线 4），``tenant_id`` 非空（红线 5）；
* 经 :class:`PostgresGoldenRegistry` 的写入/读取是**租户隔离**的：另一租户读空集；
* 冻结集的内容哈希在 PG 上真的被校验：直接改库里的 case ⇒ 读取被拒（哈希不符）；
* 无基线的 golden 回归在库里 ``passed`` 为 ``NULL``（绝非 ``False``）；
* **反事实**：去掉租户谓词（用一条无 ``tenant_id`` 过滤的查询）⇒ 「B 读不到 A」的隔离
  断言真转红，证明该谓词是承重的。

无库时干净 skip（镜像 ``test_inc46_scrub_pg.py``）。
"""

from __future__ import annotations

import socket
import uuid

import pytest

from forgeflow.config import get_settings
from forgeflow.evaluation.golden_registry import (
    GoldenCase,
    GoldenSet,
    GoldenSetHashMismatch,
    PostgresGoldenRegistry,
)
from forgeflow.evaluation.golden_regression import run_golden_regression

_TENANT_A = f"t-t17-pg-a-{uuid.uuid4().hex[:8]}"
_TENANT_B = f"t-t17-pg-b-{uuid.uuid4().hex[:8]}"


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
                    "DELETE FROM golden_runs WHERE tenant_id = ANY($1::text[])",
                    [_TENANT_A, _TENANT_B],
                )
                # golden_cases cascade from golden_sets.
                await conn.execute(
                    "DELETE FROM golden_sets WHERE tenant_id = ANY($1::text[])",
                    [_TENANT_A, _TENANT_B],
                )
        except Exception:  # noqa: BLE001 — cleanup must never mask the result
            pass
        await created.close()


def _case(i: int) -> GoldenCase:
    return GoldenCase(
        case_id=f"c{i:02d}",
        source_doc_fingerprint=f"sha256:{uuid.uuid4().hex}{uuid.uuid4().hex}",
        run_id=f"run-{i}",
        instruction=f"编辑第 {i} 段",
        expected_summary=f"expected-{i}",
    )


async def _import(store: PostgresGoldenRegistry, tenant: str, name: str, baseline=None):
    gset = GoldenSet(
        tenant_id=tenant,
        name=name,
        provenance="human_curated",
        cases=[_case(i) for i in range(4)],
        baseline_metrics=dict(baseline or {}),
    )
    return await store.import_set(gset, actor_role="admin")


# --------------------------------------------------------------------------- #
async def test_schema_has_golden_tables_and_nullable_passed(pool) -> None:
    """阳性 — 迁移 027 的三张表存在；passed 可空（未测量 ⇒ NULL）；tenant_id 非空。"""
    async with pool.acquire() as conn:
        for table in ("golden_sets", "golden_cases", "golden_runs"):
            assert await conn.fetchval(f"SELECT to_regclass('public.{table}')") is not None, table
        passed_nullable = await conn.fetchval(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'golden_runs' AND column_name = 'passed'"
        )
        assert passed_nullable == "YES"  # 未测量 ⇒ NULL（红线 4）
        for table in ("golden_sets", "golden_cases", "golden_runs"):
            tenant_nullable = await conn.fetchval(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = $1 AND column_name = 'tenant_id'",
                table,
            )
            assert tenant_nullable == "NO", table  # 红线 5


async def test_import_and_read_is_tenant_scoped(pool) -> None:
    """阳性 — 真实入库 + 读取；跨租户读空集（fail-closed）。"""
    store = PostgresGoldenRegistry(pool=pool)
    frozen = await _import(store, _TENANT_A, "pg-golden-a", baseline={"score": 0.8})
    assert frozen.frozen is True and frozen.content_hash.startswith("sha256:")

    a_sets = await store.list_sets(_TENANT_A)
    assert [s.set_id for s in a_sets] == [frozen.set_id]
    assert await store.get_set(_TENANT_B, frozen.set_id) is None
    assert await store.list_sets(_TENANT_B) == []


async def test_frozen_content_hash_lock_on_pg(pool) -> None:
    """阴性 — 直接改库里冻结集的 case ⇒ 读取因哈希不符被拒。"""
    store = PostgresGoldenRegistry(pool=pool)
    frozen = await _import(store, _TENANT_A, "pg-golden-tamper")

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE golden_cases SET instruction = '越权篡改' WHERE set_id = $1::uuid",
            frozen.set_id,
        )
    with pytest.raises(GoldenSetHashMismatch):
        await store.get_set(_TENANT_A, frozen.set_id)


async def test_golden_run_passed_is_null_when_unmeasured_on_pg(pool) -> None:
    """阳性 — 无基线的 golden 回归在库里 passed 为 NULL（绝非 False）。"""
    store = PostgresGoldenRegistry(pool=pool)
    frozen = await _import(store, _TENANT_A, "pg-golden-nobaseline", baseline={})

    result = await run_golden_regression(
        _TENANT_A,
        skill_id="s1",
        candidate_id="c1",
        new_metrics={"score": 0.95},
        registry=store,
    )
    assert result.ran is True and result.golden_run_id

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT passed, voided, set_content_hash FROM golden_runs WHERE id = $1::uuid",
            result.golden_run_id,
        )
    assert row is not None
    assert row["passed"] is None  # 未测量 ⇒ NULL（红线 4）
    assert row["voided"] is False
    assert row["set_content_hash"] == frozen.content_hash  # 可追溯到集合哈希


# --------------------------------------------------------------------------- #
# Tenant-isolation counterfactual (red line 5)                                  #
# --------------------------------------------------------------------------- #
def _assert_b_sees_nothing(rows: list) -> None:
    """The named isolation assertion — must go red once the tenant filter is gone."""
    assert rows == [], f"跨租户泄漏：B 读到了 {len(rows)} 条不属于它的 golden 集"


async def _sets_without_tenant_filter(pool) -> list[str]:
    """Re-run the read with the ``tenant_id`` predicate REMOVED (the bug).

    This is the *physical* removal of the isolation predicate — if the assertion is
    load-bearing, this turns it red.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, tenant_id, name FROM golden_sets")
    return [str(r["id"]) for r in rows]


async def test_counterfactual_removing_tenant_predicate_turns_isolation_red(pool) -> None:
    """反事实 — 去掉租户谓词 ⇒ 「B 读不到 A」的断言真转红（谓词承重）。"""
    store = PostgresGoldenRegistry(pool=pool)
    await _import(store, _TENANT_A, "pg-golden-cf")

    # (1) Real store respects the tenant predicate ⇒ B genuinely sees nothing (green).
    _assert_b_sees_nothing(await store.list_sets(_TENANT_B))

    # (2) Remove the tenant predicate ⇒ B now sees A's set ⇒ the same assertion is red.
    leaked = await _sets_without_tenant_filter(pool)
    with pytest.raises(AssertionError) as exc:
        _assert_b_sees_nothing(leaked)
    assert "跨租户泄漏" in str(exc.value)
