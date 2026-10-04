r"""T3 — 指标与成本**真的算出来了**，不是写死的 0。

"不是伪造的 0"这句话有两个方向，两个方向都要钉：

  * **不能把 0 当成算过了**：qwen3:8b 是自托管模型，成本确实是 \$0.00。但
    ``0.0`` 必须是**算**出来的（命中免费前缀），不能是函数一上来就 ``return 0``。
    反证：把**同样的 token 数**换成一个明码标价的云模型（``gpt-4o-mini``），
    成本必须 > 0。同一个函数对同一份用量给出两个不同结果 —— 这就证明了它是
    真的在算。

  * **不能把"有数据"显示成"没数据"**（反之亦然）：``has_data`` /
    ``has_cost`` / ``has_success_rate`` / ``has_latency`` 四个标志位必须由
    真实数据推导。反证：空数据集上必须全 False（若仍为 True，说明是写死的）；
    装入真实运行记录后必须按数据翻位。

token 数则用**独立重算**校验：把各次真实调用上报的 input+output 加起来，必须
等于 run 记录的 ``total_tokens``。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from tests.realstack.conftest import drive_real_run

pytestmark = pytest.mark.asyncio

_INTENT = "请核对这份对账摘要：本月应收 128 万，实收 121 万，差额待查。"


def _duration_ms(created_at: str | None, completed_at: str | None) -> float | None:
    """独立实现一遍时长计算（不与被测代码共用 helper）。"""
    if not created_at or not completed_at:
        return None
    return max(
        0.0,
        (datetime.fromisoformat(completed_at) - datetime.fromisoformat(created_at)).total_seconds()
        * 1000.0,
    )


async def test_t3_tokens_are_recomputed_from_real_usage(realstack_env):
    """token 总数必须等于各次真实调用上报之和（独立重算，不是抄字段）。"""
    run = await drive_real_run("t3", intent=_INTENT, tenant="t-realstack-t3")
    record = run["record"]
    usage = run["usage"]

    assert usage, "真实运行必须上报用量"
    recomputed = sum(
        int(e.get("input_tokens") or 0) + int(e.get("output_tokens") or 0)
        for e in usage
        if isinstance(e, dict)
    )
    assert recomputed > 0
    assert record.total_tokens == recomputed, (
        f"账本 total_tokens={record.total_tokens} 与独立重算 {recomputed} 不符"
    )
    # 每个条目都得带着模型名，否则无法定价（也就无法证明"算过了"）。
    for entry in usage:
        assert str(entry.get("model") or "").strip(), f"用量条目缺少 model，无法定价：{entry}"


async def test_t3_cost_zero_is_computed_not_hardcoded(realstack_env):
    """成本 0.0 必须是算出来的：同一个函数对云模型必须给出 > 0。"""
    from forgeflow.observability.cost_tracker import (
        calculate_cost,
        is_free_model,
    )

    run = await drive_real_run("t3", intent=_INTENT, tenant="t-realstack-t3")
    record = run["record"]
    usage = run["usage"]

    models = {str(e.get("model")) for e in usage if isinstance(e, dict)}
    assert models, "用量条目必须带 model"
    for model in models:
        assert is_free_model(model), (
            f"本机模型 {model!r} 应是自托管/免费前缀（qwen / ollama / llama …），"
            "is_free_model 判否说明定价表口径变了"
        )

    total_in = sum(int(e.get("input_tokens") or 0) for e in usage)
    total_out = sum(int(e.get("output_tokens") or 0) for e in usage)
    assert total_in + total_out > 0

    # 正向：本机模型算出来是 0（诚实——自托管边际成本真的是 0）。
    for model in models:
        assert calculate_cost(model, total_in, total_out) == 0.0
    assert record.total_cost_usd == 0.0

    # 反证（这条才是"不是伪造的 0"的硬证据）：同样的 token 数，换一个明码
    # 标价的云模型，必须 > 0。若这里也返回 0，说明函数根本没在算。
    priced = calculate_cost("gpt-4o-mini", total_in, total_out)
    assert priced > 0.0, (
        f"同样的用量（{total_in}+{total_out} tokens）在明码标价的 gpt-4o-mini 上"
        f"成本必须 > 0，实际 {priced} —— 说明 calculate_cost 是个假的 0 返回器"
    )
    # 并且应当单调：token 翻倍，成本翻倍（真的按量计价）。
    assert calculate_cost("gpt-4o-mini", total_in * 2, total_out * 2) == pytest.approx(priced * 2)


async def test_t3_has_data_flags_are_derived_from_real_data(realstack_env):
    """has_* 标志位必须由数据推导：空集全 False，装入真实记录后按数据翻位。"""
    from forgeflow.observability.metrics_source import MemoryMetricsSource
    from forgeflow.runtime.orchestrator import get_run_store, reset_run_store

    run = await drive_real_run("t3", intent=_INTENT, tenant="t-realstack-t3")
    record = run["record"]
    source = MemoryMetricsSource()

    # --- 反证 1：空数据集上必须全 False（写死的 True 会在这里暴露） ------- #
    reset_run_store()
    empty = await source.summary("t-realstack-t3")
    assert empty["total_runs"] == 0
    assert empty["has_data"] is False, "没有 run 却声称有数据 = 伪造"
    assert empty["has_success_rate"] is False, "没有终态 run，成功率无意义"
    assert empty["has_latency"] is False, "没有 completed_at，平均耗时无意义"
    assert empty["has_cost"] is False, "没有计费 run，成本无意义"

    # --- 正向：装入真实记录后必须翻位，且数值可独立重算 ------------------- #
    get_run_store().save(record)
    summary = await source.summary("t-realstack-t3")

    assert summary["total_runs"] == 1
    assert summary["has_data"] is True, (
        "库里有真实 run，has_data 仍为 False —— 前端会显示「—」，等于把有数据的看板显示成没数据"
    )
    assert summary["has_success_rate"] is True, "存在终态 run，成功率应当有意义"
    assert summary["has_latency"] is True, "存在 completed_at，平均耗时应当有意义"

    expected_latency = _duration_ms(record.created_at, record.completed_at)
    assert expected_latency is not None
    assert summary["avg_latency_ms"] == pytest.approx(expected_latency), (
        f"avg_latency_ms={summary['avg_latency_ms']} 与独立重算 "
        f"{expected_latency} 不符 —— 指标不是按真实时间戳算的"
    )

    # 本机 Ollama 是免费的 ⇒ has_cost 必须诚实为 False（不能为了"好看"翻成 True）。
    assert summary["has_cost"] is False, "自托管模型没有账单成本，has_cost 翻成 True 等于虚增花费"
    assert summary["avg_cost_usd"] == 0.0
    assert summary["source"] == "hub_runs"

    # --- 反证 2：has_data 必须与 total_runs 严格同真同假 ------------------ #
    assert summary["has_data"] == (summary["total_runs"] > 0)


async def test_t3_postgres_metrics_path_uses_the_real_run_numbers(realstack_env, pg_conn):
    """PG 指标通路：用**真实运行**的数字走一遍生产写入器，回查必须一致。"""
    import contextlib
    import uuid

    from forgeflow.observability.metrics_source import PostgresMetricsSource
    from forgeflow.observability.metrics_store import MetricsStore

    run = await drive_real_run("t3", intent=_INTENT, tenant="t-realstack-t3")
    record = run["record"]
    run_uuid = uuid.UUID(record.run_id)
    thread_uuid = uuid.UUID(record.thread_id)
    latency = _duration_ms(record.created_at, record.completed_at) or 0.0
    assert latency > 0.0, "真实运行必须有可测的端到端耗时"

    class _SingleConnPool:
        """把独立 asyncpg.Connection 适配成 MetricsStore 的 pool.acquire() 形状。

        ``MetricsStore`` 的契约是**池**（``self.pool.acquire()``）；直接喂裸
        Connection 会在 ``pool.acquire()`` 上抛 AttributeError —— 而
        ``write_metric`` 的宽 except 会把它吞成一行日志、0 行落库（这正是
        本轮实测踩到的：上一版把 pg_conn 直接传进去，回查恒空）。
        适配器保住"独立通道"语义（不复用应用自己的 pool），同时满足契约。
        """

        def __init__(self, conn):
            self._conn = conn

        @contextlib.asynccontextmanager
        async def acquire(self):
            yield self._conn

    try:
        # FK 目标：run_metrics.run_id REFERENCES workflow_runs(id)。
        await pg_conn.execute(
            """
            INSERT INTO workflow_runs
              (id, thread_id, workflow_type, status, input_data, output_data,
               total_tokens, total_cost_usd, user_id, metadata, workspace_id)
            VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8,$9,$10::jsonb,$11)
            """,
            run_uuid,
            thread_uuid,
            "generic",
            record.status,
            "{}",
            "{}",
            record.total_tokens,
            record.total_cost_usd,
            "u-realstack-qa",
            "{}",
            None,
        )
        await MetricsStore(_SingleConnPool(pg_conn)).record_run_completion(
            record.run_id,
            latency_ms=latency,
            total_tokens=record.total_tokens,
            total_cost_usd=record.total_cost_usd,
            success=(record.status == "completed"),
            agent_name="realstack",
        )

        # 独立回查：写进去的必须是真实运行的数字，不是编造的常量。
        rows = await pg_conn.fetch(
            "SELECT metric_name, metric_value FROM run_metrics WHERE run_id = $1",
            run_uuid,
        )
        found = {r["metric_name"]: float(r["metric_value"]) for r in rows}
        assert found.get("tokens_used") == float(record.total_tokens), (
            f"落库 tokens_used={found.get('tokens_used')} 与真实 run 的 "
            f"total_tokens={record.total_tokens} 不符"
        )
        assert found.get("latency_ms") == pytest.approx(latency), (
            f"落库 latency_ms={found.get('latency_ms')} 与真实耗时 {latency} 不符"
        )

        # 汇总口径：这次真实运行必须被算进去（has_data 翻 True）。
        summary = await PostgresMetricsSource(_SingleConnPool(pg_conn)).summary("t-realstack-t3")
        # INC-41 F-135 — `source` 是 API 面，保留既有取值 "postgres"；真正取数的表
        # 放在**新增**字段 `source_detail`（hub 路径 = workspace_runs）。
        assert summary["source"] == "postgres"
        assert summary["source_detail"] == "workspace_runs"
        assert summary["total_runs"] >= 1, "写入真实指标后 total_runs 仍为 0"
        assert summary["has_data"] is True
        assert summary["has_success_rate"] is True
        # workspace_runs 没有 latency 列 ⇒ 平均耗时是"未测量"，has_latency 必须
        # 如实为 False（不是被算出来的 0）。本断言钉的是真实口径，非旧契约。
        assert summary["has_latency"] is False
        assert summary["has_data"] == (summary["total_runs"] > 0)

        # has_cost 必须由"有多少条计费 run"推导 —— 用原始 SQL 独立重算一遍。
        cost_runs = await pg_conn.fetchval(
            "SELECT COUNT(*) FILTER (WHERE COALESCE(total_cost_usd, 0) > 0) FROM workflow_runs"
        )
        assert summary["has_cost"] == (int(cost_runs) > 0), (
            f"has_cost={summary['has_cost']} 与实际计费行数 {cost_runs} 不符"
        )
    finally:
        await pg_conn.execute("DELETE FROM run_metrics WHERE run_id = $1", run_uuid)
        await pg_conn.execute("DELETE FROM workflow_runs WHERE id = $1", run_uuid)
