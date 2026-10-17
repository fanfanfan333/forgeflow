"""Candidate evaluation — jump ④ (docs §2 ④ / P0-04)."""

from __future__ import annotations

import uuid

import pytest

from forgeflow.repositories.memory.skill_repo import MemorySkillCandidateRepository
from forgeflow.skills.errors import GovernanceError
from forgeflow.skills.evaluator import evaluate_candidate
from forgeflow.skills.models import SkillCandidateRecord, SkillEvaluationRecord

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
    candidate = SkillCandidateRecord(
        tenant_id=tenant,
        name="empty",
        domain="general",
        experience_ids=[],
        draft_spec={},
        status="draft",
    )
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


async def test_spec_hash_is_key_order_independent_and_tracks_nested_changes():
    from forgeflow.skills.evaluator import candidate_spec_hash

    first = {"prompt": "清洗文档", "tools": ["file.read", "file.edit"]}
    assert candidate_spec_hash(first) == candidate_spec_hash(dict(reversed(list(first.items()))))
    assert candidate_spec_hash(first) != candidate_spec_hash({**first, "tools": ["file.delete"]})


async def test_changed_candidate_cannot_reuse_passing_evaluation():
    from forgeflow.skills.governance_gate import promote_candidate

    tenant = _tenant()
    repo = MemorySkillCandidateRepository()
    candidate = SkillCandidateRecord(
        tenant_id=tenant,
        name="revision",
        experience_ids=["one", "two", "three"],
        draft_spec={
            "prompt": "read",
            "steps": ["read"],
            "tools": ["file.read"],
            "io_schema": {"input": "file"},
        },
    )
    await repo.save_candidate(candidate)
    evaluation = await evaluate_candidate(candidate.id, tenant_id=tenant, candidate_repo=repo)
    assert evaluation.verdict == "pass"
    candidate.draft_spec["prompt"] = "delete instead"
    await repo.save_evaluation(
        SkillEvaluationRecord(
            tenant_id=tenant,
            target_id=candidate.id,
            verdict="pass",
            metrics={
                **evaluation.metrics,
                "evaluation_kind": "functional",
                "functional_measured": True,
                "functional_passed": True,
                "benefit": {"positive": True},
            },
        )
    )
    await repo.save_candidate(candidate)
    with pytest.raises(GovernanceError, match="changed after evaluation"):
        await promote_candidate(candidate.id, "manager", tenant_id=tenant, candidate_repo=repo)


async def test_unchanged_evaluated_candidate_can_be_published():
    from forgeflow.repositories.memory.policy_repo import MemoryPolicyRepository
    from forgeflow.repositories.memory.skill_repo import MemorySkillRepository
    from forgeflow.skills.governance_gate import promote_candidate

    tenant = _tenant()
    repo = MemorySkillCandidateRepository()
    candidate = SkillCandidateRecord(
        tenant_id=tenant,
        name="unchanged",
        experience_ids=["one", "two", "three"],
        draft_spec={
            "prompt": "read",
            "steps": ["read"],
            "tools": ["data.query", "analysis.score", "report.render"],
            "io_schema": {"input": "file"},
        },
    )
    await repo.save_candidate(candidate)
    evaluation = await evaluate_candidate(candidate.id, tenant_id=tenant, candidate_repo=repo)
    await repo.save_evaluation(
        SkillEvaluationRecord(
            tenant_id=tenant,
            target_id=candidate.id,
            verdict="pass",
            metrics={
                **evaluation.metrics,
                "evaluation_kind": "functional",
                "functional_measured": True,
                "functional_passed": True,
                "benefit": {"positive": True},
            },
        )
    )
    version = await promote_candidate(
        candidate.id,
        "manager",
        tenant_id=tenant,
        candidate_repo=repo,
        skill_repo=MemorySkillRepository(),
        policy_repo=MemoryPolicyRepository(),
    )
    assert version.spec == candidate.draft_spec
