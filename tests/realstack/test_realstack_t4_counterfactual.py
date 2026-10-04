"""T4 — 反证注入：把 LLM 指向**不可达端口**，平台必须诚实降级。

注入方式：把 ``OLLAMA_BASE_URL`` 改成一个没人监听的地址（``127.0.0.1:1``），
再驱动一次真实 ``run_task``。可达性探测必然失败 ⇒ 模型构建降级到 mock ⇒
ReAct 循环退化到确定性计划。

期望的诚实行为（依据 ``docs/sop/INC25-DESIGN.md`` 的自有纪律）：

    「引擎/模型不可用 = **显式降级** → degraded 非空 + **运行非「已完成」**；
      **绝不**静默 ``completed/success/errors=[]``。」

所以一次降级运行必须同时满足：
  * 留下 ``degraded`` / ``error`` 标记；
  * **不得**把自己标榜成一次干干净净的成功（``outcome == "success"``
    且 ``errors == []`` 且 ``total_tokens == 0`` 三者同时出现即为"假装成功"）；
  * 降级原因必须**可读**（"exception: " 后面是空串 = 等于没记，无法排障）。

前三条是本轮的**反证钉子**：只要平台偷偷把"模型挂了"包装成"运行成功"，
这里就会红。
"""

from __future__ import annotations

import pytest

from tests.realstack.conftest import drive_real_run

pytestmark = pytest.mark.asyncio

#: 一个确定没有服务监听的地址（端口 1 需要特权，本机不可能有 Ollama 在跑）。
_UNREACHABLE = "http://127.0.0.1:1"
_INTENT = "请整理这份月度摘要并输出一段中文结论。"


async def _degraded_run(realstack_env) -> dict:
    return await drive_real_run(
        "t4",
        intent=_INTENT,
        tenant="t-realstack-t4",
        ollama_base_url=_UNREACHABLE,
    )


async def test_t4_unreachable_llm_is_detected_and_marks_degradation(realstack_env):
    """降级必须被**发现**并留下标记；同时绝不能产生假 token。"""
    run = await _degraded_run(realstack_env)
    detail, record = run["detail"], run["record"]
    llm = detail.get("llm") or {}

    # 1) 留下降级标记。
    assert llm.get("degraded"), (
        "Ollama 不可达时 run 必须带上 llm.degraded 标记；"
        f"实际 llm={ {k: v for k, v in llm.items() if k != 'plan'} }"
    )

    # 2) 构建出来的确实是 mock（真模型不可能建出来）。
    models = llm.get("models") or []
    assert models, "即便降级也要记录实际构建的模型"
    assert any(m.get("llm_type") == "mock" for m in models), (
        f"不可达端口上不该构建出真模型，实际 {models}"
    )

    # 3) 没真的调模型 ⇒ token 必须是 0，不能凭空记账。
    assert record.total_tokens == 0, f"LLM 不可达时不该产生任何 token，实际 {record.total_tokens}"
    assert not run["usage"], "降级运行不应上报任何 LLM 用量"

    # 4) 降级后仍要交付（确定性兜底真的跑了），不能空手而归。
    assert detail.get("steps"), "降级后必须回落到确定性计划并真的执行了步骤"


async def test_t4_degraded_run_must_not_advertise_a_clean_success(realstack_env):
    """【反证钉子】降级运行不得自称"成功完成"（INC25 设计文档纪律）。"""
    run = await _degraded_run(realstack_env)
    handle, detail = run["handle"], run["detail"]
    llm = detail.get("llm") or {}
    outcome = str(detail.get("outcome") or "")
    errors = list(detail.get("errors") or [])

    degraded = bool(llm.get("degraded"))
    zero_tokens = int(run["record"].total_tokens or 0) == 0

    assert not (degraded and zero_tokens and outcome == "success" and not errors), (
        "模型不可达 → 运行已降级且 token 为 0，却仍报告 "
        f"outcome={outcome!r} / status={handle.status!r} / errors=[]。"
        "docs/sop/INC25-DESIGN.md：『引擎/模型不可用 = 显式降级 → degraded 非空 + "
        "运行非「已完成」；绝不静默 completed/success/errors=[]』。"
        "一次没用上模型的运行不能在 UI 上显示成「已完成」。"
    )


async def test_t4_degradation_reason_is_diagnosable(realstack_env):
    """【反证钉子】降级原因必须可读 —— ``"exception: "`` 后面不能是空的。"""
    run = await _degraded_run(realstack_env)
    llm = run["detail"].get("llm") or {}
    reason = str(llm.get("degraded") or "")

    assert reason.strip(), "降级标记为空字符串，等于没记"
    # ``f"exception: {exc}"`` 遇到裸 ``NotImplementedError`` 时 exc 的 str 是
    # 空串，得到 "exception: " —— 没有任何排障价值。
    body = reason.split(":", 1)[-1].strip() if ":" in reason else reason.strip()
    assert body, (
        f"降级原因 {reason!r} 冒号后面是空的 —— 至少要带上异常类型 "
        '（如 f"exception: {type(exc).__name__}: {exc}"），否则线上无法定位'
    )
