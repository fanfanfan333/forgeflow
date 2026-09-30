"""INC28 W4 —— 「什么都没发生」不得报告成功：``outcome`` 是机械推导的显式结论。

## 留存经验（为什么要这条钉子）

``code.execute`` 的 ``ok`` 曾经等于 ``result.status == "ok"`` —— 而 ``status ==
"ok"`` 只说明「引擎没降级地跑完了」，**与是否产生了改动、测试是否通过无关**。于是：

* 引擎跑完但**一个字节都没改**（模型只是读文件、没动手）⇒ ``ok: True`` + 页面
  「已完成」。用户看到「成功」，仓库里却什么都没发生；
* 引擎跑完但**测试仍然是红的** ⇒ 同样 ``ok: True``。

这就是本仓反复出现的「假绿」。修法是给结果加一个**显式** ``outcome`` 字段，由
ForgeFlow 已经产出的证据**机械**推导（**不是第二次主观判断**）：

  1. 引擎降级 / 没干净跑完            ⇒ ``degraded``
  2. 评审的测试判决（``tests_verdict``）是 ``failed`` ⇒ ``failed``
  3. 根本没产生 diff                  ⇒ ``no_change``
  4. 有改动且测试不是红的             ⇒ ``succeeded``

``ok`` 只在 ``outcome == "succeeded"`` 时为真。

引文一律 ``file.py::symbol``，不用行号。
"""

from __future__ import annotations

from forgeflow.runtime import orchestrator as orch
from forgeflow.runtime.tool_handlers import (
    _CODE_OUTCOME_SUMMARY,
    _derive_code_outcome,
)

_PASSED = {"measured": True, "verdict": "passed", "passed": 3, "failed": 0}
_FAILED = {"measured": True, "verdict": "failed", "passed": 1, "failed": 2}
_A_DIFF = "diff --git a/billing/invoice.py b/billing/invoice.py\n+ ...\n"


# --------------------------------------------------------------------------- #
# 1. 机械推导：四个结论各自可达（证据 → outcome，无主观）                        #
# --------------------------------------------------------------------------- #
def test_no_change_is_not_a_success():
    """引擎 ok、测试 passed，但**没有 diff** ⇒ no_change（不是 succeeded）。"""
    assert _derive_code_outcome(
        status="ok", degraded=None, diff="", tests=_PASSED
    ) == "no_change"


def test_red_tests_are_a_failure_even_with_a_diff():
    """有改动但测试仍然红 ⇒ failed。"""
    assert _derive_code_outcome(
        status="ok", degraded=None, diff=_A_DIFF, tests=_FAILED
    ) == "failed"


def test_change_with_green_tests_is_the_only_success():
    assert _derive_code_outcome(
        status="ok", degraded=None, diff=_A_DIFF, tests=_PASSED
    ) == "succeeded"


def test_degraded_status_short_circuits_to_degraded():
    assert _derive_code_outcome(
        status="timeout", degraded=None, diff=_A_DIFF, tests=_PASSED
    ) == "degraded"
    assert _derive_code_outcome(
        status="ok", degraded="model_unavailable", diff=_A_DIFF, tests=_PASSED
    ) == "degraded"


def test_missing_verdict_with_no_diff_is_no_change():
    """没有判决（测试未执行）也不能冒充成功 —— 无改动 ⇒ no_change。"""
    assert _derive_code_outcome(
        status="ok", degraded=None, diff="", tests={"measured": False}
    ) == "no_change"


# --------------------------------------------------------------------------- #
# 2. 每个结论都有可见摘要（不会出现空 summary）                                  #
# --------------------------------------------------------------------------- #
def test_every_outcome_has_a_visible_summary():
    for outcome in ("succeeded", "failed", "no_change", "degraded"):
        assert _CODE_OUTCOME_SUMMARY.get(outcome), outcome


# --------------------------------------------------------------------------- #
# 3. 反事实注入：旧的 ``ok = (status == "ok")`` 会把「什么都没发生」判成成功      #
# --------------------------------------------------------------------------- #
def test_counterfactual_old_ok_rule_calls_no_change_a_success():
    """旧规则 ``ok = result.status == "ok"`` 对 no_change 会给出真 —— 正是假绿。"""

    def old_ok(status: str, *_a, **_k) -> bool:
        return status == "ok"

    no_change = _derive_code_outcome(
        status="ok", degraded=None, diff="", tests=_PASSED
    )
    assert no_change == "no_change"
    assert old_ok("ok") is True  # 旧规则：成功
    assert (no_change == "succeeded") is False  # 新规则：不是成功


# --------------------------------------------------------------------------- #
# 4. 装配层携带 outcome：``_assemble_codeplane`` 把 outcome 冒泡到 run 记录        #
# --------------------------------------------------------------------------- #
def test_assemble_codeplane_surfaces_outcome_from_the_code_step():
    assembled = orch._assemble_codeplane(
        [{"tool": "code.execute", "payload": {"codeplane": {"outcome": "failed"}}}],
        [],
    )
    assert assembled.get("outcome") == "failed"


def test_assemble_codeplane_prefers_the_exec_outcome_over_the_commit_one():
    """``code.execute`` 的 outcome 是权威；``code.commit`` 只在没有 execute 时兜底。"""
    assembled = orch._assemble_codeplane(
        [
            {"tool": "code.execute", "payload": {"codeplane": {"outcome": "no_change"}}},
            {"tool": "code.commit", "payload": {"codeplane": {"outcome": "succeeded"}}},
        ],
        [],
    )
    assert assembled.get("outcome") == "no_change"


def test_assemble_codeplane_without_any_outcome_does_not_invent_one():
    """老 payload（无 outcome 字段）不得凭空补一个 —— 保持缺失。"""
    assembled = orch._assemble_codeplane(
        [{"tool": "code.execute", "payload": {"codeplane": {"diff": "x"}}}],
        [],
    )
    assert "outcome" not in assembled
