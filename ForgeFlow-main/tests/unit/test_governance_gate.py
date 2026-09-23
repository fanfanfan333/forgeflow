"""Governance gate — promote requires a passing evaluation + policy allow (P0-05)."""

from __future__ import annotations

import uuid

import pytest

from forgeflow.repositories.memory.policy_repo import MemoryPolicyRepository
from forgeflow.repositories.memory.skill_repo import (
    MemorySkillCandidateRepository,
    MemorySkillRepository,
)
from forgeflow.skills.errors import GovernanceError
from forgeflow.skills.governance_gate import promote_candidate
from forgeflow.skills.models import SkillCandidateRecord, SkillEvaluationRecord

pytestmark = pytest.mark.asyncio

_DRAFT = {
    "prompt": "分析客户流失",
    "steps": ["拉取数据", "计算概率"],
    "tools": ["data.query", "analysis.score", "report.render"],
    "io_schema": {"input": {"intent": "string"}, "output": {"summary": "string"}},
}


def _tenant() -> str:
    return f"t-gate-{uuid.uuid4().hex[:8]}"


async def _candidate(repo: MemorySkillCandidateRepository, tenant: str) -> SkillCandidateRecord:
    candidate = SkillCandidateRecord(
        tenant_id=tenant, name=f"候选 {uuid.uuid4().hex[:6]}", domain="数据分析",
        experience_ids=["e1", "e2", "e3"], draft_spec=dict(_DRAFT), status="draft",
    )
    await repo.save_candidate(candidate)
    return candidate


async def _repos():
    return (
        MemorySkillCandidateRepository(),
        MemorySkillRepository(),
        MemoryPolicyRepository(),
    )


async def test_promote_requires_evaluation():
    tenant = _tenant()
    cand_repo, skill_repo, pol_repo = await _repos()
    candidate = await _candidate(cand_repo, tenant)

    with pytest.raises(GovernanceError):
        await promote_candidate(
            candidate.id, "manager-1", tenant_id=tenant,
            candidate_repo=cand_repo, skill_repo=skill_repo, policy_repo=pol_repo,
        )


async def test_promote_blocked_when_evaluation_failed():
    tenant = _tenant()
    cand_repo, skill_repo, pol_repo = await _repos()
    candidate = await _candidate(cand_repo, tenant)
    await cand_repo.save_evaluation(
        SkillEvaluationRecord(tenant_id=tenant, target_id=candidate.id, metrics={}, verdict="fail")
    )

    with pytest.raises(GovernanceError):
        await promote_candidate(
            candidate.id, "manager-1", tenant_id=tenant,
            candidate_repo=cand_repo, skill_repo=skill_repo, policy_repo=pol_repo,
        )


async def test_promote_succeeds_with_pass_and_approver():
    tenant = _tenant()
    cand_repo, skill_repo, pol_repo = await _repos()
    candidate = await _candidate(cand_repo, tenant)
    await cand_repo.save_evaluation(
        SkillEvaluationRecord(
            tenant_id=tenant, target_id=candidate.id, metrics={"score": 0.9}, verdict="pass"
        )
    )

    version = await promote_candidate(
        candidate.id, "manager-1", tenant_id=tenant,
        candidate_repo=cand_repo, skill_repo=skill_repo, policy_repo=pol_repo,
        actor_role="manager",
    )

    assert version.approved_by == "manager-1"
    assert version.semver == "0.1.0"
    assert (await cand_repo.get_candidate(tenant, candidate.id)).status == "promoted"


async def test_promote_denied_for_role_without_approve_permission():
    tenant = _tenant()
    cand_repo, skill_repo, pol_repo = await _repos()
    candidate = await _candidate(cand_repo, tenant)
    await cand_repo.save_evaluation(
        SkillEvaluationRecord(
            tenant_id=tenant, target_id=candidate.id, metrics={"score": 0.9}, verdict="pass"
        )
    )

    with pytest.raises(GovernanceError):
        await promote_candidate(
            candidate.id, "viewer-1", tenant_id=tenant,
            candidate_repo=cand_repo, skill_repo=skill_repo, policy_repo=pol_repo,
            actor_role="viewer",
        )
