"""T1 — 真实 LLM（本机 Ollama ``qwen3:8b``）真的做了规划与执行。

这条用例是"1522 例全绿但几乎全是替身"的直接反证：它不走任何 mock、不录制、
不回放，而是真的把一次任务交给 Ollama，然后断言**这次运行留下的痕迹只能由
一次真实调用产生**：

  ① run 落到明确终态（completed / failed / awaiting_approval）；
  ② ``steps`` 与 ``tool_invocations`` 都非空 —— 模型真的规划出了步骤并且
     平台真的执行了它们；
  ③ ``total_tokens > 0`` —— token 只能来自 Ollama 响应的 usage_metadata；
  ④ 已执行（``executed is True``）的调用 ``latency_ms is not None``；未执行的
     调用 ``latency_ms is None``（诚实"未测量"，不是伪造的 0）；
  ⑤ 模型产出的**内容非空字符串** —— qwen3 是 thinking 模型，一旦 thinking 打开
     就会把整个 num_predict 预算烧在隐藏思考上、返回空 content；空 content 必须
     判失败，绝不能当成"执行成功"。

另外还断言**模型身份**：构建出来的必须是 ``ChatOllama`` / ``qwen3:8b``，不能是
``MockChatModel``——否则"用了真模型"就是一句空话。
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.realstack.conftest import drive_real_run

pytestmark = pytest.mark.asyncio

_TERMINAL_STATUSES = {"completed", "failed", "awaiting_approval"}
_INTENT = (
    "请把下面这段客户反馈整理成不超过 60 字的中文要点，"
    "只输出要点本身，不要解释过程。"
)
_FEEDBACK = (
    "客户反馈：华东区 7 月交付延迟 3 天，主要原因是仓库拣货排队；"
    "客户希望后续提供预计发货时间，并愿意为加急服务付费。"
)


async def test_t1_real_ollama_drives_the_run_to_a_terminal_state(realstack_env):
    """① 终态 ② steps / tool_invocations 非空。"""
    run = await drive_real_run(
        "t1",
        intent=_INTENT,
        tenant="t-realstack-t1",
        context={"text": _FEEDBACK},
    )
    handle, detail = run["handle"], run["detail"]

    assert handle.status in _TERMINAL_STATUSES, (
        f"真实 run 必须落到明确终态，实际 status={handle.status!r} "
        f"（detail.outcome={detail.get('outcome')!r}）"
    )
    assert handle.detail["outcome"] in {"success", "partial", "failure", "aborted"}

    steps = detail.get("steps") or []
    invocations = detail.get("tool_invocations") or []
    assert steps, "真实 LLM 必须规划出至少一个步骤（steps 为空 = 没规划）"
    assert invocations, (
        "平台必须真的执行了工具调用（tool_invocations 为空 = 没执行）"
    )
    # 运行级 1:1：steps 与 invocation 一一对应（设计 §11-C）。
    assert len(steps) == len(invocations), (
        f"steps({len(steps)}) 与 tool_invocations({len(invocations)}) 应 1:1"
    )


async def test_t1_real_ollama_model_identity_and_tokens(realstack_env):
    """③ total_tokens > 0 —— 而且模型必须是真的 ChatOllama，不是 mock。"""
    settings = realstack_env
    run = await drive_real_run(
        "t1",
        intent=_INTENT,
        tenant="t-realstack-t1",
        context={"text": _FEEDBACK},
    )
    detail, record = run["detail"], run["record"]
    llm = detail.get("llm") or {}

    # --- 模型身份：真实栈的硬证据 ----------------------------------------- #
    models = llm.get("models") or []
    assert models, "run 必须记录本次实际构建的模型（llm.models 为空 = 无据可查）"
    llm_types = {m.get("llm_type") for m in models}
    assert "mock" not in llm_types, (
        f"配置为 ollama 却构建出 mock 模型：{models}。"
        "这是静默降级，真实栈用例必须判失败。"
    )
    assert any(str(m.get("llm_type")) == "chat-ollama" for m in models), (
        f"至少有一个槽位必须是真的 ChatOllama，实际 {models}"
    )
    assert any(
        str(m.get("model")) == str(settings.ollama_model) for m in models
    ), f"实际使用的模型应为 {settings.ollama_model!r}，实际 {models}"
    assert not llm.get("degraded"), (
        f"Ollama 可达时不该出现降级标记：{llm.get('degraded')!r}"
    )

    # --- 真实 token：只能来自 Ollama 的 usage_metadata --------------------- #
    usage: list[dict[str, Any]] = run["usage"]
    assert usage, "真实调用必须上报 usage（llm_usage 为空 = 没真的调模型）"
    recomputed = sum(
        int(e.get("input_tokens") or 0) + int(e.get("output_tokens") or 0)
        for e in usage
        if isinstance(e, dict)
    )
    assert recomputed > 0, f"上报的 token 数必须 > 0，实际 {recomputed}（{usage}）"
    assert record.total_tokens == recomputed, (
        f"run 记录的 total_tokens({record.total_tokens}) 必须等于各次真实调用 "
        f"上报之和({recomputed}) —— 不等说明账本不是从真实用量算出来的"
    )
    assert detail["total_tokens"] > 0


async def test_t1_latency_is_measured_not_fabricated(realstack_env):
    """④ 已执行的调用 latency_ms 不是 None；未执行的诚实留空。"""
    run = await drive_real_run(
        "t1",
        intent=_INTENT,
        tenant="t-realstack-t1",
        context={"text": _FEEDBACK},
    )
    invocations = run["detail"].get("tool_invocations") or []

    executed = [i for i in invocations if i.get("executed") is True]
    assert executed, (
        "真实运行至少要有一个真正执行成功的工具调用，"
        f"实际 invocation 状态={[i.get('status') for i in invocations]}"
    )
    for inv in executed:
        assert inv.get("latency_ms") is not None, (
            f"已执行的调用 {inv.get('tool')!r} 必须测到 latency_ms，"
            "实际为 None（没测就写 0 是伪造数据）"
        )
        assert float(inv["latency_ms"]) >= 0.0

    # 反面：没执行的调用必须留 None，绝不能写成 0 假装测过。
    for inv in invocations:
        if inv.get("executed") is not True:
            assert inv.get("latency_ms") is None, (
                f"未执行的调用 {inv.get('tool')!r} 的 latency_ms 应为 None（未测量），"
                f"实际 {inv.get('latency_ms')!r}"
            )


async def test_t1_model_content_is_not_empty(realstack_env):
    """⑤ 模型产出的内容必须是非空字符串（qwen3 thinking 抽干预算 ⇒ 判失败）。"""
    run = await drive_real_run(
        "t1",
        intent=_INTENT,
        tenant="t-realstack-t1",
        context={"text": _FEEDBACK},
    )
    detail = run["detail"]
    llm = detail.get("llm") or {}

    final_answer = llm.get("final_answer")
    assert final_answer is not None, (
        "ReAct 循环必须以模型的最终答复收敛（final_answer 为 None = 循环没走到终点）"
    )
    text = str((final_answer or {}).get("text") or "").strip()
    assert text, (
        "模型返回内容为空字符串 —— qwen3:8b 一旦开启 thinking 就会把整个 "
        "num_predict 预算烧在隐藏思考上并返回空 content（done_reason=length）。"
        "空内容绝不能被当成执行成功，必须判失败。"
    )
    assert llm.get("iterations", 0) >= 1, "至少发生了一次真实模型调用"

    # 最终交付物（report.render 的产物）也必须真的有内容。
    artifacts = detail.get("artifacts") or []
    assert artifacts, "真实运行必须产出至少一个交付物"
    assert str(artifacts[0].get("content") or "").strip(), (
        "交付物内容为空 —— 报告渲染没拿到任何真实结果"
    )
