"""INC2-13 — pre-publication trust baseline (review finding #8).

Three checks: tool whitelist ⊆ allowed set, no privilege escalation, DlpGate
finds no PII. Hooked into ``promote_candidate`` so a spec that fails any of
them is a 403 with a concrete reason.
"""

from __future__ import annotations

import pytest

from forgeflow.skills.errors import GovernanceError
from forgeflow.skills.trust_baseline import TrustReport, allowed_tool_set, verify_trust_baseline


def test_clean_spec_passes_all_three_checks():
    report = verify_trust_baseline(
        {"tools": ["data.query", "analysis.score"], "prompt": "生成销售报告"},
        ["write:skills"],
    )
    assert report.ok is True
    assert report.failures == []
    assert report.checks == {
        "tools_whitelisted": True,
        "no_privilege_escalation": True,
        "no_pii": True,
    }


def test_tool_outside_whitelist_fails():
    report = verify_trust_baseline({"tools": ["evil.exec"]}, ["*:*"])
    assert report.ok is False
    assert report.checks["tools_whitelisted"] is False
    assert "evil.exec" in report.reason


def test_privilege_escalation_fails():
    report = verify_trust_baseline(
        {"required_permissions": ["write:policies", "approve:skills"]},
        ["read:skills"],
    )
    assert report.ok is False
    assert report.checks["no_privilege_escalation"] is False
    assert "write:policies" in report.reason


def test_pii_in_spec_fails_dlp():
    report = verify_trust_baseline(
        {"prompt": "联系张三 邮箱 zhangsan@example.com 手机号 13812345678"},
        ["*:*"],
    )
    assert report.ok is False
    assert report.checks["no_pii"] is False
    assert "敏感信息" in report.reason


def test_catalogue_contains_the_platform_tools():
    """The whitelist is the real platform catalogue, not an invented one."""
    catalogue = allowed_tool_set()
    for tool in ("research.search", "data.query", "analysis.score", "docs.parse", "report.render"):
        assert tool in catalogue, tool


async def test_promote_is_blocked_by_the_baseline(monkeypatch):
    """A candidate whose spec declares an off-catalogue tool cannot be promoted."""
    from forgeflow.repositories.memory.policy_repo import MemoryPolicyRepository
    from forgeflow.repositories.memory.skill_repo import MemorySkillCandidateRepository
    from forgeflow.skills.governance_gate import promote_candidate
    from forgeflow.skills.models import SkillCandidateRecord, SkillEvaluationRecord

    tenant = "t-trust"
    cand_repo = MemorySkillCandidateRepository()
    cand = SkillCandidateRecord(
        tenant_id=tenant,
        name="越权技能",
        domain="general",
        draft_spec={"tools": ["evil.exec"], "prompt": "做点坏事"},
    )
    await cand_repo.save_candidate(cand)
    await cand_repo.save_evaluation(
        SkillEvaluationRecord(
            tenant_id=tenant, target_id=cand.id, metrics={"score": 0.9}, verdict="pass"
        )
    )

    with pytest.raises(GovernanceError) as excinfo:
        await promote_candidate(
            cand.id,
            "manager-1",
            tenant_id=tenant,
            candidate_repo=cand_repo,
            skill_repo=None,
            policy_repo=MemoryPolicyRepository(),
            actor_role="manager",
        )

    assert excinfo.value.status_code == 403
    assert "可信基线校验未通过" in str(excinfo.value)
    assert "evil.exec" in str(excinfo.value)


async def test_promote_still_succeeds_for_a_clean_spec(monkeypatch):
    """No regression: a spec inside the catalogue promotes as before."""
    from forgeflow.repositories.memory.policy_repo import MemoryPolicyRepository
    from forgeflow.repositories.memory.skill_repo import (
        MemorySkillCandidateRepository,
        MemorySkillRepository,
    )
    from forgeflow.skills.governance_gate import promote_candidate
    from forgeflow.skills.models import SkillCandidateRecord, SkillEvaluationRecord

    tenant = "t-trust-ok"
    cand_repo = MemorySkillCandidateRepository()
    cand = SkillCandidateRecord(
        tenant_id=tenant,
        name="合规技能",
        domain="sales",
        draft_spec={"tools": ["data.query", "report.render"], "prompt": "生成销售报告"},
    )
    await cand_repo.save_candidate(cand)
    await cand_repo.save_evaluation(
        SkillEvaluationRecord(
            tenant_id=tenant, target_id=cand.id, metrics={"score": 0.9}, verdict="pass"
        )
    )

    version = await promote_candidate(
        cand.id,
        "manager-1",
        tenant_id=tenant,
        candidate_repo=cand_repo,
        skill_repo=MemorySkillRepository(),
        policy_repo=MemoryPolicyRepository(),
        actor_role="manager",
    )

    assert version.skill_id
    assert version.spec["tools"] == ["data.query", "report.render"]
