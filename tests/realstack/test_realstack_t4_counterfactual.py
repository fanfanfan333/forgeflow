"""T4 — 反证注入：把 LLM 指向**不可达端口**，平台必须诚实降级。

注入方式：把 ``OLLAMA_BASE_URL`` 改成一个没人监听的地址（``127.0.0.1:1``），
再驱动一次真实 ``run_task``。可达性探测必然失败 ⇒ 模型构建降级到 mock ⇒
ReAct 循环退化到确定性计划。

期望的诚实行为（FF-R1 B1 起的现行契约，取代旧的「显式降级」口径）：

    「配置的 provider 不可用 = **显式失败**：status=failed、
      terminated_by=model_unavailable、failure_code=model_unavailable、
      model_calls=0、llm.degraded 为空；**绝不**静默
      completed/success/errors=[]，也绝不拿确定性桩的结果冒充交付。」

所以一次模型不可达的运行必须同时满足：
  * 以 model_unavailable 显式失败（handle.status == "failed"）；
  * **不得**把自己标榜成一次干干净净的成功（``outcome == "success"``
    且 ``errors == []`` 且 ``total_tokens == 0`` 三者同时出现即为"假装成功"）；
  * 失败原因必须**可诊断**（failure_code / failure_reason /
    terminated_by 都要能直接指出是模型不可用，而不是一句空串）。

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
    """模型不可达必须被**发现**并显式失败；同时绝不能产生假 token。"""
    run = await _degraded_run(realstack_env)
    detail, record = run["detail"], run["record"]
    handle = run["handle"]
    llm = detail.get("llm") or {}

    # 1) 显式失败，而不是旧口径的「降级后继续交付」。
    assert handle.status == "failed", (
        f"配置的 provider 不可达必须显式失败，实际 status={handle.status!r}"
    )
    assert llm.get("terminated_by") == "model_unavailable", (
        f"terminated_by 必须是 model_unavailable，实际 llm={ {k: v for k, v in llm.items() if k != 'plan'} }"
    )
    assert llm.get("failure_code") == "model_unavailable", (
        f"failure_code 必须是 model_unavailable，实际 {llm.get('failure_code')!r}"
    )

    # 2) 不再写 degraded 标记 —— 显式失败取代了降级口径。
    assert not llm.get("degraded"), (
        f"显式失败的运行不应再带 degraded 标记，实际 {llm.get('degraded')!r}"
    )

    # 3) 构建出来的确实是 mock（真模型不可能建出来），且这一点有记录。
    models = llm.get("models") or []
    assert models, "即便失败也要记录实际构建的模型"
    assert any(m.get("llm_type") == "mock" for m in models), (
        f"不可达端口上不该构建出真模型，实际 {models}"
    )

    # 4) 没真的调模型 ⇒ token 必须是 0，不能凭空记账。
    assert record.total_tokens == 0, f"LLM 不可达时不该产生任何 token，实际 {record.total_tokens}"
    assert not run["usage"], "失败运行不应上报任何 LLM 用量"


async def test_t4_degraded_run_must_not_advertise_a_clean_success(realstack_env):
    """【反证钉子】模型不可达的运行不得自称"成功完成"。"""
    run = await _degraded_run(realstack_env)
    handle, detail = run["handle"], run["detail"]
    outcome = str(detail.get("outcome") or "")
    errors = list(detail.get("errors") or [])

    zero_tokens = int(run["record"].total_tokens or 0) == 0

    assert not (zero_tokens and outcome == "success" and not errors), (
        "模型不可达 → 一次模型调用都没有、token 为 0，却仍报告 "
        f"outcome={outcome!r} / status={handle.status!r} / errors=[]。"
        "现行契约（FF-R1 B1）：配置的 provider 不可用必须显式失败"
        "（model_unavailable），绝不静默 completed/success/errors=[]。"
        "一次没用上模型的运行不能在 UI 上显示成「已完成」。"
    )
    assert handle.status == "failed" and outcome != "success", (
        f"显式失败应同时落在 status 与 outcome 上，实际 status={handle.status!r} outcome={outcome!r}"
    )


async def test_t4_degradation_reason_is_diagnosable(realstack_env):
    """【反证钉子】失败原因必须可诊断 —— 一眼能看出是模型不可用、配的是谁。"""
    run = await _degraded_run(realstack_env)
    llm = run["detail"].get("llm") or {}

    assert llm.get("failure_code") == "model_unavailable", (
        f"failure_code 必须直指 model_unavailable，实际 {llm.get('failure_code')!r}"
    )
    assert str(llm.get("terminated_by") or "").strip() == "model_unavailable"
    reason = str(llm.get("failure_reason") or "")
    assert reason.strip(), "failure_reason 为空，等于没记"
    body = reason.split(":", 1)[-1].strip() if ":" in reason else reason.strip()
    assert body, (
        f"失败原因 {reason!r} 冒号后面是空的 —— 至少要带上原因码"
        "（如 failed_retryable），否则线上无法定位"
    )
