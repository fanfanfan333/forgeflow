"""Skill Candidate Compiler — jump ③ (docs §2.1 / P0-02)."""

from __future__ import annotations

import uuid

import pytest

from forgeflow.experience.embedding import deterministic_embedding
from forgeflow.experience.models import ExperienceRecord
from forgeflow.repositories.memory.experience_repo import MemoryExperienceRepository
from forgeflow.repositories.memory.skill_repo import MemorySkillCandidateRepository
from forgeflow.skills.candidate_compiler import compile_candidate, compile_candidate_or_raise
from forgeflow.skills.errors import InsufficientExperiencesError

pytestmark = pytest.mark.asyncio

_TEXT = "分析销售数据 数据分析"


def _tenant() -> str:
    return f"t-cand-{uuid.uuid4().hex[:8]}"


async def _seed(repo: MemoryExperienceRepository, tenant: str, n: int) -> list[ExperienceRecord]:
    embedding = deterministic_embedding(_TEXT)
    records = []
    for i in range(n):
        rec = ExperienceRecord(
            tenant_id=tenant,
            run_id=f"r-{i}",
            summary=f"{_TEXT} — 共执行 2 个步骤，结果：success",
            outcome="success",
            reusable_steps=[{"tool": "data.query"}, {"tool": "report.render"}],
            tags=["数据分析"],
            embedding=embedding,
        )
        await repo.save(rec)
        records.append(rec)
    return records


async def test_compiles_draft_with_four_elements_when_enough_similar():
    tenant = _tenant()
    exp_repo = MemoryExperienceRepository()
    cand_repo = MemorySkillCandidateRepository()
    await _seed(exp_repo, tenant, 3)

    candidate = await compile_candidate(
        tenant, mode="auto", experience_repo=exp_repo, candidate_repo=cand_repo
    )

    assert candidate.status == "draft"
    spec = candidate.draft_spec
    for key in ("prompt", "steps", "tools", "io_schema"):
        assert spec.get(key), f"draft_spec.{key} should be non-empty"
    assert candidate.similarity_score > 0.85
    # N:M provenance persisted
    linked = await cand_repo.list_candidate_experiences(tenant, candidate.id)
    assert len(linked) >= 3


async def test_insufficient_when_too_few_similar():
    tenant = _tenant()
    exp_repo = MemoryExperienceRepository()
    cand_repo = MemorySkillCandidateRepository()
    await _seed(exp_repo, tenant, 1)

    candidate = await compile_candidate(
        tenant, mode="auto", experience_repo=exp_repo, candidate_repo=cand_repo
    )

    assert candidate.status == "insufficient"
    assert candidate.draft_spec == {}
    # not persisted
    assert await cand_repo.get_candidate(tenant, candidate.id) is None


async def test_compile_or_raise_raises_on_insufficient():
    tenant = _tenant()
    exp_repo = MemoryExperienceRepository()
    cand_repo = MemorySkillCandidateRepository()

    with pytest.raises(InsufficientExperiencesError):
        await compile_candidate_or_raise(
            tenant, mode="auto", experience_repo=exp_repo, candidate_repo=cand_repo
        )


async def test_manual_mode_uses_supplied_experiences():
    tenant = _tenant()
    exp_repo = MemoryExperienceRepository()
    cand_repo = MemorySkillCandidateRepository()
    records = await _seed(exp_repo, tenant, 3)

    candidate = await compile_candidate(
        tenant,
        [r.id for r in records],
        mode="manual",
        experience_repo=exp_repo,
        candidate_repo=cand_repo,
    )

    assert candidate.status == "draft"
    assert set(candidate.experience_ids) == {r.id for r in records}
