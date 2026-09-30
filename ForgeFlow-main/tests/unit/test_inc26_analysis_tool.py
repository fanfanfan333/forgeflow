"""INC26 T03 — `analysis.profile` 真实读到夹具 CSV 的 R（行数）与 A（聚合）。

**不自证恒真**：夹具用一条**与 `csv` 模块无关**的路径手算 ground truth（按已知行元组
直接 `float()` 求和、`len()` 计行），再用另一条路径（真实 handler）断言两者相等。夹具
数字预先是整数/整半，避免浮点噪声。

诚实纪律一并钉死：

* 未测量（缺 paths / 缺 column / 文件读不到）⇒ `rows` / `aggregate_value` 为 `None`，
  **绝不写 0**；且经 `ToolExecutor` 记为 `blocked`（不计 failed，`latency_ms is None`）。
* 列不存在 ⇒ `rows`（真测量）保留、`aggregate_value` 为 `None` 并带逐字 note。

引文纪律：一律 `file.py::symbol`，不用行号。
"""

from __future__ import annotations

import pytest

from forgeflow.runtime.tool_handlers import analysis_profile
from forgeflow.runtime.tool_executor import ToolCallContext, ToolExecutor
from forgeflow.runtime import tool_registry

pytestmark = pytest.mark.asyncio

#: 夹具内容 —— 三行数据，金额列可手算求和。
_HEADER = ("lead_id", "company", "amount")
_ROWS: tuple[tuple[str, str, str], ...] = (
    ("1", "Acme", "100.5"),
    ("2", "Globex", "200.25"),
    ("3", "Initech", "300.25"),
)


def _ctx() -> ToolCallContext:
    return ToolCallContext(
        run_id="run-inc26-profile",
        step_id="run-inc26-profile:0:0",
        tenant_id="t-inc26",
        user_id="u-inc26",
        role="manager",
        intent="分析数据文件",
        args={},
    )


def _write_fixture(tmp_path) -> tuple[str, int, float, list[str]]:
    """写一个已知 CSV，并**手算** ground truth（与 csv 模块无关的路径）。"""
    text = "\n".join([",".join(_HEADER), *[",".join(r) for r in _ROWS]]) + "\n"
    path = tmp_path / "leads.csv"
    path.write_text(text, encoding="utf-8")
    # ground truth：直接对已知元组计算，绝不调用被测实现。
    expected_rows = len(_ROWS)
    expected_sum = sum(float(row[2]) for row in _ROWS)  # 100.5 + 200.25 + 300.25
    return str(path), expected_rows, expected_sum, list(_HEADER)


async def test_binding_is_real_stdlib_csv():
    binding = tool_registry.resolve("analysis.profile")
    assert binding is not None, "analysis.profile 没有绑定 —— 成了孤儿/死绑定"
    assert binding.kind == "real"
    assert binding.provider == "stdlib-csv"
    assert callable(binding.handler)


async def test_profile_reads_real_rows_and_aggregate(tmp_path):
    path, expected_rows, expected_sum, expected_cols = _write_fixture(tmp_path)
    out = await analysis_profile({"paths": [path], "column": "amount"}, _ctx())

    assert out["ok"] is True
    assert out["provider"] == "stdlib-csv"
    assert out["rows"] == expected_rows
    assert out["columns"] == expected_cols
    assert out["aggregate_column"] == "amount"
    assert out["aggregate_value"] == pytest.approx(expected_sum)


async def test_missing_paths_is_not_executed_and_never_zero():
    out = await analysis_profile({"paths": [], "column": "amount"}, _ctx())
    assert out["ok"] is False
    assert out.get("not_executed") is True
    assert out["rows"] is None, "未测量 rows 绝不能写 0"
    assert out["aggregate_value"] is None, "未测量 aggregate 绝不能写 0"


async def test_missing_column_is_not_executed():
    out = await analysis_profile({"paths": ["whatever.csv"]}, _ctx())
    assert out["ok"] is False
    assert out.get("not_executed") is True
    assert out["aggregate_value"] is None


async def test_unreadable_file_is_not_executed(tmp_path):
    missing = tmp_path / "does_not_exist.csv"
    out = await analysis_profile({"paths": [str(missing)], "column": "amount"}, _ctx())
    assert out["ok"] is False
    assert out.get("not_executed") is True
    assert out["rows"] is None and out["aggregate_value"] is None


async def test_unknown_column_keeps_rows_but_null_aggregate(tmp_path):
    path, expected_rows, _sum, _cols = _write_fixture(tmp_path)
    out = await analysis_profile({"paths": [path], "column": "not_a_column"}, _ctx())
    assert out["ok"] is True
    assert out["rows"] == expected_rows  # 行数真的测到了
    assert out["aggregate_value"] is None  # 聚合列不存在 ⇒ 不猜，null
    assert "not_a_column" in out["note"]


async def test_blocked_through_executor_is_not_a_failure():
    """缺输入 ⇒ 经 ToolExecutor 记为 `blocked`（不计 failed，latency None）。"""
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()
    try:
        invocation = await ToolExecutor().execute("analysis.profile", ctx=_ctx())
    finally:
        tool_registry.reset_registry()
        tool_registry.load_default_bindings()

    assert invocation.status == "blocked"
    assert invocation.executed is False
    assert invocation.latency_ms is None
    assert invocation.error is None  # blocked 不是 failed，不写 error
