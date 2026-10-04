"""T2 — 真的落到 PostgreSQL，并且**跨重启**还能查回来。

分两段证明，缺一不可：

  A. **独立 asyncpg 回查**：用一条**不属于应用**的连接（自己的
     ``asyncpg.connect``，不是 ``forgeflow.database.get_pool``，也不是仓库对象）
     直接 ``SELECT``。用应用自己的 store 读回来只能证明"in-process 缓存里有"，
     证明不了"落盘"。

  B. **跨重启**：把所有进程内状态清空（run store / workspace store / 仓储缓存 /
     asyncpg 连接池），模拟一次进程重启，然后重新取值。此时 in-process 的
     ``MemoryRunStore`` 必然是空的——如果还能查到，那只能来自 PostgreSQL。

被验证的两张表：
  * ``workspace_runs``（migration 016）—— run 头信息 / 会话关系 / 产物；
  * ``experiences`` —— 经验抽取（闭环 jump ②）真的写了库。
"""

from __future__ import annotations

import pytest

from tests.realstack.conftest import drive_real_run

pytestmark = pytest.mark.asyncio

_INTENT = "请把这份季度摘要归档：华东区销量同比上升，华南持平，华下降。"


def _reset_every_in_process_cache() -> None:
    """模拟进程重启：清掉一切可能"替 PostgreSQL 作答"的内存状态。"""
    import forgeflow.database as _db
    from forgeflow.config import get_settings
    from forgeflow.observability.metrics_source import reset_metrics_source
    from forgeflow.repositories.factory import reset_repositories
    from forgeflow.runtime.orchestrator import reset_run_store
    from forgeflow.workspace.store import reset_workspace_store

    reset_run_store()
    reset_workspace_store()
    reset_repositories()
    reset_metrics_source()
    _db._pool = None  # noqa: SLF001 — 连接池也换掉，逼下一次重新建连
    assert str(get_settings().storage_backend).lower() == "postgres"


async def test_t2_run_header_is_really_in_postgres(realstack_env, pg_conn):
    """A. 独立 asyncpg 通道能查到这次真实运行的头信息。"""
    run = await drive_real_run(
        "t2",
        intent=_INTENT,
        tenant="t-realstack-t2",
    )
    handle, record = run["handle"], run["record"]

    row = await pg_conn.fetchrow(
        "SELECT run_id, tenant_id, session_id, actor_user_id, actor_role, "
        "       workflow_type, status, outcome, intent "
        "FROM workspace_runs WHERE run_id = $1",
        handle.run_id,
    )
    assert row is not None, f"workspace_runs 里查不到 run {handle.run_id} —— 这次运行没有真的落盘"
    assert row["run_id"] == handle.run_id
    assert row["tenant_id"] == "t-realstack-t2", f"持久化丢了租户隔离：{row['tenant_id']!r}"
    assert row["status"] == record.status, (
        f"落盘 status({row['status']!r}) 与内存记录({record.status!r}) 不一致"
    )
    assert row["outcome"] == record.outcome
    assert row["actor_user_id"] == "u-realstack-qa"
    assert row["actor_role"] == "admin"
    assert row["session_id"] == record.session_id
    assert _INTENT in str(row["intent"]), "落盘的意图文本被改写/截断"


async def test_t2_experience_row_is_really_in_postgres(realstack_env, pg_conn):
    """A. 闭环 jump ②：经验抽取真的写进了 ``experiences``。"""
    run = await drive_real_run(
        "t2",
        intent=_INTENT,
        tenant="t-realstack-t2",
    )
    record = run["record"]
    assert record.experience_id, "真实运行必须抽出一条经验"

    row = await pg_conn.fetchrow(
        "SELECT id, run_id, tenant_id, outcome, summary, tags FROM experiences WHERE run_id = $1",
        run["handle"].run_id,
    )
    assert row is not None, (
        f"experiences 里查不到 run {run['handle'].run_id} 的经验 —— "
        "经验抽取没真的写库（STORAGE_BACKEND=postgres 时应当是 Pg 仓储）"
    )
    assert str(row["id"]) == str(record.experience_id)
    assert row["tenant_id"] == "t-realstack-t2"
    assert row["outcome"] == record.outcome
    assert str(row["summary"]).strip(), "经验摘要为空"


async def test_t2_header_survives_a_simulated_restart(realstack_env, pg_conn):
    """B. 跨重启：清掉全部内存状态后，头信息仍能从 PostgreSQL 查回。"""
    run = await drive_real_run(
        "t2",
        intent=_INTENT,
        tenant="t-realstack-t2",
    )
    handle, record = run["handle"], run["record"]

    _reset_every_in_process_cache()

    # 反证的前置条件：进程内的 run store 确实已经空了。
    from forgeflow.runtime.orchestrator import get_run_store

    assert get_run_store().get(handle.run_id) is None, (
        "模拟重启后 in-process run store 应为空；不为空说明没真的清干净，"
        "后面「还能查到」就不能证明落盘"
    )

    # 重新取值 —— 必须真的走 PostgreSQL 实现。
    from forgeflow.workspace.store import get_workspace_store

    store = get_workspace_store()
    assert type(store).__name__ == "PgWorkspaceStore", (
        f"storage_backend=postgres 时应当拿到 PgWorkspaceStore，"
        f"实际 {type(store).__name__}（拿到内存实现 = 这条用例根本没有测到 PG）"
    )

    restored = await store.get("t-realstack-t2", handle.run_id)
    assert restored is not None, f"模拟重启后查不回 run {handle.run_id} —— 它没有真的持久化"
    assert restored.run_id == handle.run_id
    assert restored.status == record.status
    assert restored.outcome == record.outcome
    assert restored.tenant_id == "t-realstack-t2"
    assert _INTENT in restored.intent

    # 会话关系也必须活下来（INC32 ADR-02：Follow-up 链要靠它）。
    assert restored.session_id == record.session_id
