"""INC8-A5 — the HITL trigger conditions are configurable policy, not hard-coded
constants, and every decision carries an auditable *basis* (review finding ⑤).

Contract pinned here:
  * the default classification is byte-identical to the historical sets;
  * a config override (``HIGH_RISK_ACTIONS`` / ``HIGH_RISK_RESOURCES``) takes
    effect immediately;
  * the matched rule is reported via ``risk_basis`` (present and non-empty).
"""

from __future__ import annotations

import pytest

from forgeflow.governance.policy_engine import (
    PolicyEngine,
    assess_risk,
    classify_risk,
)
from forgeflow.repositories.memory.policy_repo import MemoryPolicyRepository


def test_default_classification_is_unchanged():
    assert classify_risk("transfer", "execute") == "high"
    assert classify_risk("skills", "delete") == "high"
    assert classify_risk("email", "send") == "high"
    assert classify_risk("skills", "write") == "medium"
    assert classify_risk("metrics", "read") == "low"


def test_default_sets_match_the_historical_constants():
    from forgeflow.config import get_settings

    settings = get_settings()
    assert settings.high_risk_action_set() == frozenset(
        {"delete", "drop", "destroy", "truncate", "purge", "revoke"}
    )
    assert settings.high_risk_resource_set() == frozenset(
        {"transfer", "payment", "pay", "funds", "wire", "payout"}
    )


def test_a_word_outside_the_default_set_is_not_high():
    assert classify_risk("gift", "execute") == "low"


def test_config_override_adds_a_resource(monkeypatch):
    from forgeflow.config import get_settings

    monkeypatch.setattr(get_settings(), "high_risk_resources", "transfer,gift")

    assert classify_risk("gift", "execute") == "high"
    assessment = assess_risk("gift", "execute")
    assert assessment.level == "high"
    assert "gift" in assessment.basis


def test_config_override_adds_an_action(monkeypatch):
    from forgeflow.config import get_settings

    monkeypatch.setattr(get_settings(), "high_risk_actions", "delete,exfiltrate")

    assert classify_risk("reports", "exfiltrate") == "high"


def test_caller_flag_basis_is_reported():
    assessment = assess_risk("workflows", "execute", {"sensitive": True})
    assert assessment.level == "high"
    assert assessment.basis and "caller flag" in assessment.basis


@pytest.mark.asyncio
async def test_decision_carries_non_empty_risk_basis():
    engine = PolicyEngine(repo=MemoryPolicyRepository())
    decision = await engine.evaluate(
        subject="admin",
        resource="transfer",
        action="execute",
        context={"role": "admin"},
        tenant_id="t-inc8-a5",
    )
    assert decision.risk_level == "high"
    assert decision.risk_basis and decision.risk_basis.strip()


@pytest.mark.asyncio
async def test_tool_call_basis_names_the_matched_rule():
    engine = PolicyEngine(repo=MemoryPolicyRepository())
    decision = await engine.evaluate_tool_call(
        "admin-1", "admin", "payment.transfer", tenant_id="t-inc8-a5", run_id="r-1"
    )
    assert decision.risk_level == "high"
    assert decision.risk_basis
    assert ("transfer" in decision.risk_basis) or ("payment" in decision.risk_basis)
