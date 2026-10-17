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


async def test_no_recorded_tools_cannot_generate_fictional_skill_steps():
    tenant = _tenant()
    experiences = MemoryExperienceRepository()
    candidates = MemorySkillCandidateRepository()
    records = await _seed(experiences, tenant, 3)
    for record in records:
        record.reusable_steps = []
        await experiences.save(record)
    result = await compile_candidate(
        tenant, mode="auto", experience_repo=experiences, candidate_repo=candidates
    )
    assert result.status == "insufficient"
    assert result.draft_spec == {}


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
    linked = await cand_repo.list_candidate_experiences(tenant, candidate.id)
    assert len(linked) == len(records)


async def test_repeated_ids_do_not_satisfy_minimum_experience_count():
    tenant = _tenant()
    repo = MemoryExperienceRepository()
    records = await _seed(repo, tenant, 1)
    candidate = await compile_candidate(
        tenant,
        [records[0].id] * 20,
        mode="manual",
        experience_repo=repo,
        candidate_repo=MemorySkillCandidateRepository(),
    )
    assert candidate.status == "insufficient"
    assert candidate.experience_ids == [records[0].id]


async def test_concurrent_compilation_reuses_one_draft_and_releases_lock():
    import asyncio

    from forgeflow.skills.candidate_compiler import _compile_locks

    tenant = _tenant()
    repo = MemoryExperienceRepository()
    candidates = MemorySkillCandidateRepository()
    await _seed(repo, tenant, 3)
    results = await asyncio.gather(
        *[
            compile_candidate(tenant, experience_repo=repo, candidate_repo=candidates)
            for _ in range(4)
        ]
    )
    assert len({candidate.id for candidate in results}) == 1
    assert len(await candidates.list_candidates(tenant)) == 1
    assert (asyncio.get_running_loop(), tenant) not in _compile_locks


async def test_same_sources_reuse_draft_but_other_tenant_does_not():
    tenant = _tenant()
    repo = MemoryExperienceRepository()
    candidates = MemorySkillCandidateRepository()
    records = await _seed(repo, tenant, 3)
    first = await compile_candidate(tenant, experience_repo=repo, candidate_repo=candidates)
    second = await compile_candidate(
        tenant,
        [r.id for r in reversed(records)],
        mode="manual",
        experience_repo=repo,
        candidate_repo=candidates,
    )
    assert first.id == second.id
    assert len(await candidates.list_candidates(tenant)) == 1
    assert await candidates.list_candidates(_tenant()) == []


async def test_slow_optional_model_draft_does_not_block_loop_and_has_deadline(monkeypatch):
    import asyncio
    import threading

    from forgeflow.config import get_settings
    from forgeflow.skills import candidate_compiler as compiler

    release = threading.Event()
    started = threading.Event()

    def blocked_draft(*args):
        started.set()
        release.wait(2)
        return None

    monkeypatch.setattr(compiler, "_llm_draft", blocked_draft)
    monkeypatch.setattr(get_settings(), "skill_candidate_draft_timeout_seconds", 0.05)
    tenant = _tenant()
    repo = MemoryExperienceRepository()
    await _seed(repo, tenant, 3)
    task = asyncio.create_task(
        compile_candidate(
            tenant,
            experience_repo=repo,
            candidate_repo=MemorySkillCandidateRepository(),
        )
    )
    try:
        for _ in range(20):
            if started.is_set():
                break
            await asyncio.sleep(0.005)
        assert started.is_set()
        assert not task.done()
        result = await asyncio.wait_for(task, 0.5)
        assert result.status == "draft" and result.draft_spec["tools"]
    finally:
        release.set()
