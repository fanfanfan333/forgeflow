"""INC46 T08 — candidate gate / critic enhancement.

Scope (阳性 / 阴性 / 反事实 / 可复用接口):

* **阳性** — DANGEROUS 工具候选 ⇒ 有效风险升级为 ``high`` 且
  ``blocks_auto_publish=True``；联锁自检探针 ``INTERLOCK_PROBE()`` 报告 ``ok``。
* **阴性** — 缺失败路径（``missing_failure_path``）/ 无终止条件（``no_termination``）
  永远只是 critic 的 ``medium``；闸门层必须把它们当成**阻断项**（``allowed=False``）——
  「只记录 finding 而放行」是不允许的。
* **反事实（真跑）** — 摘掉 critic 的 ``dangerous_operation`` 判定后，阳性断言（闸门
  命名阻断 + ``allowed=False``）必须转红；证明该判定是载荷承重的，不是 vacuous。
* **可复用接口** — ``detect_conflicts`` 是**独立于 critic** 的函数，可直接在 critic
  之外调用（T35 复用），接受 ``SkillContract`` / 契约 dict / 七段文档。

Every test drives the real functions (never a re-implementation).
"""

from __future__ import annotations

import json

from forgeflow.skills import candidate_gates
from forgeflow.skills import critic
from forgeflow.skills import risk_escalation
from forgeflow.skills.candidate_gates import (
    CONFLICT_CODES,
    GATE_BLOCKING_CODES,
    INTERLOCK_PROBE,
    CandidateGateResult,
    blocks_auto_publish,
    detect_conflicts,
    evaluate_candidate_gate,
)
from forgeflow.skills.contracts import SkillContract
from forgeflow.skills.critic import critique


# --------------------------------------------------------------------------- #
# fixtures — the exact seams the real code touches                             #
# --------------------------------------------------------------------------- #
def _dangerous_contract() -> SkillContract:
    """A money-movement skill that *declares* low risk — the escalation trigger."""
    return SkillContract(
        goal="转账付款",
        procedure=["读取金额", "发起转账"],
        tools=["payment.transfer"],
        verification=["失败则回滚"],
        risk_level="low",
    )


def _clean_contract() -> SkillContract:
    """A fully-declared, read-only, when-to-use-explicit contract (no findings)."""
    return SkillContract(
        goal="当用户需要生成周报时使用",
        procedure=["读取数据", "渲染周报"],
        tools=["report.render"],
        inputs={"topic": "str"},
        outputs={"path": "str"},
        policies=["仅读"],
        verification=["输出文件存在"],
    )


# --------------------------------------------------------------------------- #
# 阳性 — DANGEROUS ⇒ 升级 high + 阻断自动发布                                   #
# --------------------------------------------------------------------------- #
def test_positive_dangerous_candidate_escalates_to_high_and_blocks_auto_publish():
    result = evaluate_candidate_gate(_dangerous_contract())

    assert result.allowed is False, "DANGEROUS 候选必须被闸门阻断"
    assert result.risk_level == "high", "DANGEROUS 工具 ⇒ 有效风险必须升级为 high"
    assert result.declared_risk == "low", "声明仍被如实回显（不被篡改）"
    assert result.escalated is True
    assert result.dangerous_tools == ["payment.transfer"]
    assert "dangerous_operation" in result.blocking_codes
    assert result.blocks_auto_publish is True, "DANGEROUS 候选不得自动发布"


def test_positive_risk_escalation_module_is_the_single_escalation_source():
    """``effective = max(declared, derived_from_tools)`` —— DANGEROUS ⇒ high。"""
    esc = risk_escalation.risk_escalation(_dangerous_contract())
    assert (esc.declared, esc.effective, esc.escalated) == ("low", "high", True)
    assert esc.reasons, "升级必须附带可读理由"

    assert risk_escalation.effective_risk_level(_dangerous_contract()) == "high"
    assert risk_escalation.requires_human_signoff(_dangerous_contract()) is True
    assert risk_escalation.blocks_auto_publish(_dangerous_contract()) is True
    # 闸门便捷函数与风险升级一致
    assert candidate_gates.blocks_auto_publish(_dangerous_contract()) is True
    assert blocks_auto_publish(_dangerous_contract()) is True


def test_positive_gate_result_is_json_serialisable_and_reports_the_decision():
    result = evaluate_candidate_gate(_dangerous_contract())
    assert isinstance(result, CandidateGateResult)
    payload = result.to_dict()
    json.dumps(payload)  # 必须可 JSON 序列化（HTTP 响应形状）
    assert payload["blocks_auto_publish"] is True
    assert payload["allowed"] is False
    assert payload["risk_level"] == "high"
    assert "dangerous_operation" in payload["blocking_codes"]
    assert payload["findings"], "findings 必须逐条透传（供审阅/UI 渲染）"


def test_interlock_probe_reports_ok_for_the_t15_forward_contract():
    probe = INTERLOCK_PROBE()
    assert probe["ok"] is True
    assert str(probe["evidence"]).strip(), "证据栏不得为空"


# --------------------------------------------------------------------------- #
# 阴性 — 缺失败路径 / 无终止条件 ⇒ 闸门必须阻断，不得只记录而放行                 #
# --------------------------------------------------------------------------- #
def test_negative_write_without_failure_path_is_blocked_not_passed():
    contract = SkillContract(
        goal="当用户需要编辑文档时使用",
        procedure=["打开文档", "修改内容"],
        tools=["document.edit"],  # WRITE（有副作用）
        inputs={"path": "str"},
        outputs={"path": "str"},
        verification=["输出文件存在，内容正确"],  # 无任何失败 / 回滚 / 重试信号
    )
    crit = critique(contract)
    codes = [f["code"] for f in crit.findings]
    assert "missing_failure_path" in codes, "含写工具却无失败路径 ⇒ 必须产生 finding"
    # critic 层只是 medium：must_fix 不含它 —— 单靠 must_fix 不会阻断
    assert crit.severity == "medium"
    assert "missing_failure_path" not in crit.must_fix

    # 闸门层必须把它当阻断项：不得「只记录 finding 而放行」
    gate = evaluate_candidate_gate(contract)
    assert gate.allowed is False
    assert "missing_failure_path" in gate.blocking_codes
    assert gate.blocks_auto_publish is True


def test_negative_runaway_procedure_without_termination_is_blocked_not_passed():
    contract = SkillContract(
        goal="当用户需要生成周报时使用",
        procedure=[f"步骤 {i}" for i in range(critic.MAX_PROCEDURE_STEPS + 4)],
        tools=["report.render"],
        inputs={"topic": "str"},
        outputs={"path": "str"},
        verification=["输出文件存在"],
    )
    crit = critique(contract)
    assert "no_termination" in [f["code"] for f in crit.findings]
    assert crit.severity == "medium"
    assert "no_termination" not in crit.must_fix

    gate = evaluate_candidate_gate(contract)
    assert gate.allowed is False
    assert "no_termination" in gate.blocking_codes


def test_negative_gate_blocking_codes_are_exactly_the_two_safety_invariants():
    assert GATE_BLOCKING_CODES == frozenset({"missing_failure_path", "no_termination"})


# --------------------------------------------------------------------------- #
# 反事实（真跑） — 移除 dangerous_operation 判定 ⇒ 阳性转红                       #
# --------------------------------------------------------------------------- #
def test_counterfactual_removing_dangerous_rule_turns_the_positive_red(monkeypatch):
    dangerous = _dangerous_contract()

    # (0) 阳性基线：闸门必须命名阻断 dangerous_operation 且不放行
    baseline = evaluate_candidate_gate(dangerous)
    assert baseline.allowed is False
    assert "dangerous_operation" in baseline.blocking_codes

    # (1) 反事实：真跑到 critic 内层，摘掉 dangerous_operation 判定
    original = critic._privilege_findings

    def _without_dangerous(contract):
        return [f for f in original(contract) if f.get("code") != "dangerous_operation"]

    monkeypatch.setattr(critic, "_privilege_findings", _without_dangerous)

    # (2) 同一阳性断言现在必须转红：闸门不再命名阻断项，allowed 翻 True
    mutated = evaluate_candidate_gate(dangerous)
    assert "dangerous_operation" not in mutated.blocking_codes, "判定已被摘除，不应仍出现"
    assert mutated.allowed is True, "缺了该判定，闸门这一层就放行了（阳性转红）"

    # (3) 诚实说明：纵深防御仍在 —— risk_escalation 独立把 DANGEROUS 抬到 high，
    #     所以「自动发布」仍被阻断。证明安全网不依赖单条 critic 规则。
    assert mutated.risk_level == "high"
    assert mutated.blocks_auto_publish is True


# --------------------------------------------------------------------------- #
# 可复用接口 — detect_conflicts 独立于 critic，可直接外部调用（T35 复用）        #
# --------------------------------------------------------------------------- #
def test_conflict_detection_is_standalone_and_reusable():
    left = SkillContract(
        goal="生成周报",
        applicable_when={"domain": "报表"},
        inputs={"topic": "str"},
        outputs={"path": "str"},
    )
    right = SkillContract(
        goal="生成月报",
        applicable_when={"domain": "报表"},
        inputs={"topic": "int"},
        outputs={"path": "str"},
    )

    # 直接调用（不经 critic / 不经闸门）——证明接口是独立函数
    findings = detect_conflicts(left, right)
    codes = {f["code"] for f in findings}
    assert codes == {"io_type_conflict", "overlapping_trigger"}
    assert CONFLICT_CODES == ("io_type_conflict", "overlapping_trigger")

    io = next(f for f in findings if f["code"] == "io_type_conflict")
    assert io["severity"] == "high" and io["field"] == "inputs.topic"
    trigger = next(f for f in findings if f["code"] == "overlapping_trigger")
    assert trigger["severity"] == "medium"
    assert set(findings[0]) == {"code", "severity", "message", "field"}

    # 阴性：目标相同 + 无共享触发 ⇒ 无冲突（证明不是永真）
    same = SkillContract(goal="生成周报", inputs={"topic": "str"}, outputs={"path": "str"})
    assert detect_conflicts(left, same) == []


def test_conflict_detection_accepts_plain_contract_dicts():
    a = {"goal": "A", "inputs": {"x": "str"}}
    b = {"goal": "B", "inputs": {"x": "int"}}
    codes = {f["code"] for f in detect_conflicts(a, b)}
    assert codes == {"io_type_conflict"}


# --------------------------------------------------------------------------- #
# 新增 critic code — body_over_limit / description_missing_when_to_use（可加不回退）#
# --------------------------------------------------------------------------- #
def test_new_body_over_limit_code_fires_on_oversized_body():
    contract = SkillContract(
        goal="当用户需要生成周报时使用",
        procedure=["读取数据", "渲染周报"],
        tools=["report.render"],
        inputs={"topic": "str"},
        outputs={"path": "str"},
        policies=[f"约束条目 {i}：保持格式一致" for i in range(520)],  # > 500 行
        verification=["输出文件存在"],
    )
    crit = critique(contract)
    finding = next(f for f in crit.findings if f["code"] == "body_over_limit")
    assert finding["severity"] == "medium"
    assert "body_over_limit" not in crit.must_fix, "非 high ⇒ 不进 must_fix"

    gate = evaluate_candidate_gate(contract)
    assert "body_over_limit" not in gate.blocking_codes, "体量提示不阻断闸门"


def test_new_when_to_use_heuristic_fires_on_bare_capability_and_clears_on_trigger():
    bare = SkillContract(
        goal="生成周报",  # 只写「做什么」
        procedure=["渲染周报"],
        tools=["report.render"],
        inputs={"topic": "str"},
        outputs={"path": "str"},
        verification=["输出文件存在"],
    )
    finding = next(
        f for f in critique(bare).findings if f["code"] == "description_missing_when_to_use"
    )
    assert finding["severity"] == "low", "启发式恒为 low（误报可控，不阻断）"

    # 显式「何时用」信号 ⇒ 清干净（中英文 + applicable_when 三条路径）
    for explicit in (
        SkillContract(goal="当用户需要生成周报时使用", applicable_when={}),
        SkillContract(goal="Use when the user needs a weekly report"),
        SkillContract(goal="生成周报", applicable_when={"domain": "报表"}),
    ):
        codes = [f["code"] for f in critique(explicit).findings]
        assert "description_missing_when_to_use" not in codes, explicit.goal


def test_existing_critic_behaviour_is_unchanged_by_the_additive_codes():
    # 干净契约：T08 新码不得误报，既有 code 也不多不少
    clean = critique(_clean_contract())
    assert clean.findings == [], clean.findings
    assert clean.severity == "none"

    # 既有 high code 仍走原机制进入 must_fix（未被 T08 改动）
    bad = critique(SkillContract())
    assert bad.severity == "high"
    assert "goal_missing" in bad.must_fix
    assert "procedure_missing" in bad.must_fix
    assert "tools_missing" in bad.must_fix


# --------------------------------------------------------------------------- #
# 联锁锚点 — R1（forgeflow.skills.candidate_gate）表面等价                       #
# --------------------------------------------------------------------------- #
def test_candidate_gate_anchor_reexports_the_public_surface():
    from forgeflow.skills import candidate_gate as anchor

    for name in (
        "GATE_BLOCKING_CODES",
        "CONFLICT_CODES",
        "CandidateGateResult",
        "evaluate_candidate_gate",
        "blocks_auto_publish",
        "detect_conflicts",
        "INTERLOCK_PROBE",
    ):
        assert hasattr(anchor, name), f"锚点缺少 {name}"

    assert anchor.evaluate_candidate_gate(_dangerous_contract()).blocks_auto_publish is True
    assert anchor.INTERLOCK_PROBE()["ok"] is True
