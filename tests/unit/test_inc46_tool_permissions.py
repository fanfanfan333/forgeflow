"""INC46 T04 — four-level tool privilege classification (design §1.4 / §2.4).

Proves the classification is **derived** from the runtime tool table (no
scattered, hand-maintained ``{tool: class}`` copy) and covers the whole
``PLATFORM_PLAN_TOOLS ∪ TOOL_PERMISSION_MAP`` set with no omissions. Also pins
the four additive ``critic`` finding codes this task introduces.

Runs on the memory profile — no PostgreSQL, no network, no LLM.
"""

from __future__ import annotations

import pytest

from forgeflow.runtime import gate
from forgeflow.runtime.gate import PLATFORM_PLAN_TOOLS, TOOL_PERMISSION_MAP
from forgeflow.skills import critic as critic_mod
from forgeflow.skills import tool_permissions as tp
from forgeflow.skills.contracts import SkillContract

_ALL_DECLARED_TOOLS = frozenset(PLATFORM_PLAN_TOOLS) | frozenset(TOOL_PERMISSION_MAP)


def _contract(**overrides) -> SkillContract:
    base = dict(
        goal="分析客户流失",
        procedure=["拉取数据", "计算概率", "生成建议"],
        tools=["data.query", "analysis.score", "report.render"],
        inputs={"intent": "string"},
        outputs={"summary": "string"},
        verification=["输出非空"],
        risk_level="low",
    )
    base.update(overrides)
    return SkillContract(**base)


# --------------------------------------------------------------------------- #
# 1. Full coverage — no tool in the declared set is left unclassified          #
# --------------------------------------------------------------------------- #


def test_every_declared_tool_is_classified():
    assert _ALL_DECLARED_TOOLS, "反空转：声明集合为空"
    for tool in sorted(_ALL_DECLARED_TOOLS):
        assert tp.classify_tool(tool) in tp.TOOL_CLASSES, tool


def test_classify_tool_is_total_over_unknown_names():
    # Recognised tail classifies; the unrecognised tail fails closed to READ.
    assert tp.classify_tool("") == tp.READ
    assert tp.classify_tool("totally.unknown.verb") == tp.READ
    assert tp.classify_tool("acme.widget") == tp.READ


# --------------------------------------------------------------------------- #
# 2. Derivation — the class comes from the gate table, not a frozen copy        #
# --------------------------------------------------------------------------- #


def test_dangerous_set_is_derived_from_gate_table():
    expected = frozenset(TOOL_PERMISSION_MAP) | {"code.commit"}
    assert tp.DANGEROUS_TOOLS() == expected
    # Non-vacuous: the money/egress/privilege ids really are DANGEROUS.
    for tool in sorted(TOOL_PERMISSION_MAP):
        assert tp.classify_tool(tool) == tp.DANGEROUS, tool
    assert tp.classify_tool("code.commit") == tp.DANGEROUS


def test_classification_tracks_the_gate_table(monkeypatch):
    """A key added to the gate table is picked up — proving derivation.

    If the code carried its own privilege list, this new id would fall through
    to ``READ``; because the class is derived from ``TOOL_PERMISSION_MAP``, it
    becomes ``DANGEROUS`` immediately.
    """
    assert tp.classify_tool("evil.transfer") == tp.READ
    monkeypatch.setitem(gate.TOOL_PERMISSION_MAP, "evil.transfer", ("approve", "proposals"))
    assert tp.classify_tool("evil.transfer") == tp.DANGEROUS


# --------------------------------------------------------------------------- #
# 3. The ordered rule set — one representative per class                       #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "tool,expected",
    [
        # Rule ③ — side-effecting verbs.
        ("document.edit", tp.WRITE),
        ("textfile.edit", tp.WRITE),
        ("sheet.edit", tp.WRITE),
        ("pdf.generate", tp.WRITE),
        ("artifact.save", tp.WRITE),
        # Rule ④ — read / compute verbs.
        ("document.inspect", tp.READ),
        ("textfile.inspect", tp.READ),
        ("sheet.inspect", tp.READ),
        ("pdf.inspect", tp.READ),
        ("analysis.score", tp.READ),
        ("analysis.profile", tp.READ),
        ("docs.parse", tp.READ),
        ("report.render", tp.READ),
        ("policy.check", tp.READ),
        ("git.diff", tp.READ),
        ("code.lint", tp.READ),
        ("code.run", tp.READ),
        ("code.execute", tp.READ),
    ],
)
def test_rule_based_classification(tool: str, expected: str):
    assert tp.classify_tool(tool) == expected


def test_research_namespace_is_external():
    # Rule ② keys on the namespace, so any research.* id is EXTERNAL.
    assert tp.classify_tool("research.search") == tp.EXTERNAL
    assert tp.classify_tool("research.synthetic") == tp.EXTERNAL


def test_write_action_verbs_are_recognised():
    for verb in ("edit", "write", "save", "generate", "apply", "commit"):
        assert tp.classify_tool(f"widget.{verb}") == tp.WRITE, verb


# --------------------------------------------------------------------------- #
# 4. class_of_skill / risk_level_from_classes                                   #
# --------------------------------------------------------------------------- #


def test_class_of_skill_flat_shape_and_aggregate():
    spec = {
        "tools": ["data.query", "research.search", "document.edit", "data.query"],
    }
    result = tp.class_of_skill(spec)
    # per-tool entries (deduped) ...
    assert result["data.query"] == tp.READ
    assert result["research.search"] == tp.EXTERNAL
    assert result["document.edit"] == tp.WRITE
    # ... plus the two aggregates
    assert result["overall"] == tp.EXTERNAL  # EXTERNAL(2) > WRITE(1) > READ(0)
    assert result["risk_level"] == "medium"


def test_class_of_skill_accepts_object_and_none():
    assert tp.class_of_skill(None) == {"overall": tp.READ, "risk_level": "low"}
    assert tp.class_of_skill(_contract())["overall"] == tp.READ


def test_class_of_skill_overall_is_dangerous_for_a_granted_tool():
    result = tp.class_of_skill({"tools": ["data.query", "payment.transfer"]})
    assert result["overall"] == tp.DANGEROUS
    assert result["risk_level"] == "high"


@pytest.mark.parametrize(
    "classes,expected",
    [
        ({}, "low"),
        ({"a": "READ", "b": "READ"}, "low"),
        ({"a": "READ", "b": "WRITE"}, "medium"),
        ({"a": "READ", "b": "EXTERNAL"}, "medium"),
        ({"a": "WRITE", "b": "DANGEROUS"}, "high"),
    ],
)
def test_risk_level_from_classes(classes: dict, expected: str):
    assert tp.risk_level_from_classes(classes) == expected


def test_risk_level_ignores_non_class_values():
    # Passing the whole class_of_skill result (which carries risk_level) is safe.
    assert tp.risk_level_from_classes({"overall": "READ", "risk_level": "low"}) == "low"
    assert tp.risk_level_from_classes(["nonsense"]) == "low"


def test_class_order_matches_tool_classes():
    assert set(tp.CLASS_ORDER) == set(tp.TOOL_CLASSES)
    assert tp.CLASS_ORDER[tp.READ] < tp.CLASS_ORDER[tp.WRITE]
    assert tp.CLASS_ORDER[tp.WRITE] < tp.CLASS_ORDER[tp.EXTERNAL]
    assert tp.CLASS_ORDER[tp.EXTERNAL] < tp.CLASS_ORDER[tp.DANGEROUS]


# --------------------------------------------------------------------------- #
# 5. critic — the four additive codes, and the pre-existing rules untouched     #
# --------------------------------------------------------------------------- #


def _codes(crit) -> set[str]:
    return {f["code"] for f in crit.findings}


def test_critic_clean_contract_has_no_privilege_findings():
    crit = critic_mod.critique(_contract())  # all-READ, verified, 3 steps
    assert crit.must_fix == []
    assert crit.severity in ("none", "low", "medium")
    assert _codes(crit).isdisjoint(
        {"dangerous_operation", "missing_failure_path", "no_termination", "insufficient_boundary"}
    )


def test_critic_dangerous_tool_without_hitl_or_verification_is_high():
    crit = critic_mod.critique(
        _contract(tools=["data.query", "payment.transfer"], verification=[], risk_level="low")
    )
    assert "dangerous_operation" in _codes(crit)
    assert "dangerous_operation" in crit.must_fix  # high ⇒ must_fix (existing mechanism)


def test_critic_dangerous_tool_needs_both_hitl_and_verification():
    # Verification present but risk not high ⇒ still flagged (missing HITL).
    crit = critic_mod.critique(
        _contract(tools=["payment.transfer"], verification=["转账回执非空"], risk_level="low")
    )
    assert "dangerous_operation" in _codes(crit)
    # Both HITL (risk high) and verification ⇒ clean of the dangerous finding.
    ok = critic_mod.critique(
        _contract(
            tools=["payment.transfer"],
            verification=["转账失败回滚", "回执非空"],
            risk_level="high",
        )
    )
    assert "dangerous_operation" not in _codes(ok)


def test_critic_flags_missing_failure_path_for_side_effecting_tools():
    crit = critic_mod.critique(
        _contract(tools=["document.edit"], verification=["输出非空"])  # no failure token
    )
    assert "missing_failure_path" in _codes(crit)
    # A declared rollback path clears it.
    ok = critic_mod.critique(_contract(tools=["document.edit"], verification=["写入失败回滚"]))
    assert "missing_failure_path" not in _codes(ok)


def test_critic_flags_insufficient_boundary_for_side_effecting_tools():
    crit = critic_mod.critique(_contract(tools=["document.edit"], verification=[]))
    assert "insufficient_boundary" in _codes(crit)
    assert "insufficient_boundary" not in _codes(
        critic_mod.critique(_contract(tools=["document.edit"], verification=["边界校验"]))
    )


def test_critic_flags_no_termination_for_runaway_procedure():
    long_procedure = [f"步骤 {i}" for i in range(critic_mod.MAX_PROCEDURE_STEPS + 1)]
    crit = critic_mod.critique(_contract(procedure=long_procedure))
    assert "no_termination" in _codes(crit)
    assert "no_termination" not in _codes(
        critic_mod.critique(_contract(procedure=long_procedure[: critic_mod.MAX_PROCEDURE_STEPS]))
    )


def test_critic_preexisting_codes_and_severities_unchanged():
    # The INC43 red lines must still hold — additive change only.
    clean = critic_mod.critique(_contract())
    assert clean.must_fix == []

    off = critic_mod.critique(_contract(tools=["data.query", "evil.exfiltrate"]))
    assert "tools_not_whitelisted" in off.must_fix

    missing = critic_mod.critique(_contract(goal="", procedure=[], tools=[]))
    assert {"goal_missing", "procedure_missing", "tools_missing"} <= set(missing.must_fix)
    assert missing.severity == "high"

    hitl = critic_mod.critique(_contract(risk_level="high", verification=[]))
    assert "high_risk_unverified" in hitl.must_fix
