"""Candidate evaluation — jump ④ (docs §2 ④ / P0-04)."""

from __future__ import annotations

import uuid

import pytest

from forgeflow.repositories.memory.skill_repo import MemorySkillCandidateRepository
from forgeflow.skills.errors import GovernanceError
from forgeflow.skills.evaluator import evaluate_candidate
from forgeflow.skills.models import SkillCandidateRecord

pytestmark = pytest.mark.asyncio


def _tenant() -> str:
    return f"t-eval-{uuid.uuid4().hex[:8]}"


async def test_evaluation_passes_for_complete_draft():
    tenant = _tenant()
    repo = MemorySkillCandidateRepository()
    candidate = SkillCandidateRecord(
        tenant_id=tenant,
        name="客户流失分析 技能候选",
        domain="数据分析",
        experience_ids=["e1", "e2", "e3"],
        draft_spec={
            "prompt": "分析流失风险",
            "steps": ["拉取数据", "计算概率"],
            "tools": ["data.query", "analysis.score", "report.render"],
            "io_schema": {"input": {"intent": "string"}, "output": {"summary": "string"}},
        },
        similarity_score=0.97,
        status="draft",
    )
    await repo.save_candidate(candidate)

    evaluation = await evaluate_candidate(candidate.id, tenant_id=tenant, candidate_repo=repo)

    assert evaluation.verdict == "pass"
    assert evaluation.metrics["score"] >= 0.6
    assert evaluation.target_id == candidate.id
    # verdict reflected back onto the candidate
    assert (await repo.get_candidate(tenant, candidate.id)).status == "evaluating"


async def test_evaluation_fails_for_empty_draft():
    tenant = _tenant()
    repo = MemorySkillCandidateRepository()
    candidate = SkillCandidateRecord(tenant_id=tenant, name="empty", domain="general",
                                     experience_ids=[], draft_spec={}, status="draft")
    await repo.save_candidate(candidate)

    evaluation = await evaluate_candidate(candidate.id, tenant_id=tenant, candidate_repo=repo)

    assert evaluation.verdict == "fail"
    assert (await repo.get_candidate(tenant, candidate.id)).status == "rejected"


async def test_evaluation_missing_candidate_raises_404():
    tenant = _tenant()
    repo = MemorySkillCandidateRepository()
    with pytest.raises(GovernanceError) as exc:
        await evaluate_candidate("does-not-exist", tenant_id=tenant, candidate_repo=repo)
    assert exc.value.status_code == 404
