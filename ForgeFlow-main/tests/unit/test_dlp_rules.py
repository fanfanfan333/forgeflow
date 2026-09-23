"""INC2-11 — configurable DLP rules (docs/sop/05-ARCHITECTURE-INC2.md §2.8).

Covers: the built-in default set still blocks ``cn_id``/``email`` (QA P1-1),
custom JSON rules take effect, overrides replace a category, and the gate stays
fail-closed (DLP_ENABLED=false detects but does not block).
"""

from __future__ import annotations

import json
from pathlib import Path

from forgeflow.governance.dlp import DlpGate
from forgeflow.governance.dlp_rules import (
    DEFAULT_RULES,
    DlpRuleSet,
    clear_rule_set_cache,
    load_rule_set,
)

_CN_ID = "110101199003071234"


def _write_rules(tmp_path: Path, payload: dict) -> str:
    path = tmp_path / "dlp_rules.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_default_set_includes_cn_id_and_email():
    categories = DlpRuleSet.default().categories
    assert "cn_id" in categories
    assert "email" in categories
    # Luhn-validated credit-card is always applied by the redactor.
    assert "credit_card" in categories


def test_default_rules_declares_cn_id():
    declared = {rule.category for rule in DEFAULT_RULES}
    assert "cn_id" in declared


def test_default_scan_blocks_cn_id_and_email():
    text, matches = DlpRuleSet.default().scan(
        f"身份证 {_CN_ID} 邮箱 alice@example.com"
    )
    categories = {m.category for m in matches}
    assert "cn_id" in categories
    assert "email" in categories
    assert _CN_ID not in text
    assert "alice@example.com" not in text


def test_custom_rule_from_file_takes_effect(tmp_path):
    path = _write_rules(
        tmp_path,
        {
            "rules": [
                {"category": "employee_id", "pattern": r"EMP-\d{6}", "description": "员工编号"}
            ]
        },
    )
    rule_set = DlpRuleSet.from_file(path)
    text, matches = rule_set.scan("员工 EMP-123456 报到")
    assert "EMP-123456" not in text
    assert any(m.category == "employee_id" for m in matches)
    assert "employee_id" in rule_set.categories


def test_custom_file_keeps_builtin_fail_closed(tmp_path):
    path = _write_rules(
        tmp_path, {"rules": [{"category": "employee_id", "pattern": r"EMP-\d{6}"}]}
    )
    rule_set = DlpRuleSet.from_file(path)
    # Built-in categories must still fire even though a custom file is loaded.
    _, matches = rule_set.scan(f"邮箱 alice@example.com 证件 {_CN_ID}")
    categories = {m.category for m in matches}
    assert {"email", "cn_id"} <= categories


def test_override_existing_category(tmp_path):
    path = _write_rules(
        tmp_path, {"rules": [{"category": "phone", "pattern": r"\bHOTLINE-\d{4}\b"}]}
    )
    rule_set = DlpRuleSet.from_file(path)
    text, matches = rule_set.scan("call HOTLINE-9000 now")
    assert "HOTLINE-9000" not in text
    assert any(m.category == "phone" for m in matches)


def test_missing_file_degrades_to_defaults(tmp_path):
    rule_set = DlpRuleSet.from_file(str(tmp_path / "does_not_exist.json"))
    assert "cn_id" in rule_set.categories


def test_gate_uses_injected_rule_set(tmp_path):
    path = _write_rules(
        tmp_path, {"rules": [{"category": "employee_id", "pattern": r"EMP-\d{6}"}]}
    )
    gate = DlpGate(rules=DlpRuleSet.from_file(path))
    result = gate.scan("EMP-123456")
    assert result.pii_found is True
    assert "employee_id" in result.categories


def test_gate_default_still_blocks_cn_id():
    gate = DlpGate()
    result = gate.scan(f"客户身份证 {_CN_ID}")
    assert result.pii_found is True
    assert "cn_id" in result.categories


def test_gate_disabled_detects_but_does_not_block():
    gate = DlpGate(enabled=False)
    scan = gate.scan(f"身份证 {_CN_ID}")
    assert scan.pii_found is True  # detection is independent of the switch
    outbound = gate.scan_outbound(f"身份证 {_CN_ID}", channel="http")
    assert outbound.blocked is False


def test_load_rule_set_default_when_no_path():
    clear_rule_set_cache()
    rule_set = load_rule_set(None)
    assert "cn_id" in rule_set.categories
