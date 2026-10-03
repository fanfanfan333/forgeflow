"""INC44 T01 (integration) — synthesize → loop → publish closes end to end.

The unit pin (``tests/unit/test_inc44_skill_publish.py``) proves the loop now
persists a passing evaluation and advances the candidate. This integration test
proves the **two existing routes合二为一即闭环** (design §1.1): a real compiled
candidate, run through the loop, then published through the *unmodified*
``governance_gate`` — with a semver-valid version whose ``eval_score`` carries the
loop's measured score.

Runs on the **memory** profile (``STORAGE_BACKEND=memory``) — no PostgreSQL, no
network, no LLM. The experience seed uses the deterministic offline embedding.
"""

from __future__ import annotations

import uuid

import pytest

from forgeflow.experience.embedding import deterministic_embedding
from forgeflow.experience.models import ExperienceRecord
from forgeflow.repositories.memory.experience_repo import MemoryExperienceRepository
from forgeflow.repositories.memory.policy_repo import MemoryPolicyRepository
from forgeflow.repositories.memory.skill_repo import (
    MemorySkillCandidateRepository,
    MemorySkillRepository,
    clear_skill_store,
)
from forgeflow.skills import engineering as eng


def _tenant() -> str:
    return f"t-inc44i-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _clean_store():
    clear_skill_store()
    yield
    clear_skill_store()


async def _seed_experiences(repo: MemoryExperienceRepository, tenant: str, n: int):
    text = "分析销售数据 数据分析"
    embedding = deterministic_embedding(text)
    for i in range(n):
        await repo.save(
            ExperienceRecord(
                tenant_id=tenant,
                run_id=f"r-{i}",
                summary=f"{text} — 共执行 2 个步骤，结果：success",
                outcome="success",
                reusable_steps=[{"tool": "data.query"}, {"tool": "report.render"}],
                tags=["数据分析"],
                embedding=embedding,
            )
        )


async def test_loop_then_publish_closes_the_skill_lifecycle():
    """synthesize → run_engineering_loop → version_and_publish succeeds."""
    tenant = _tenant()
    exp_repo = MemoryExperienceRepository()
    cand_repo = MemorySkillCandidateRepository()
    skill_repo = MemorySkillRepository()
    pol_repo = MemoryPolicyRepository()
    await _seed_experiences(exp_repo, tenant, 3)

    contract = await eng.synthesize(
        tenant, mode="auto", candidate_repo=cand_repo, experience_repo=exp_repo
    )
    assert contract.is_complete() is True
    candidates = await cand_repo.list_candidates(tenant)
    assert candidates
    candidate_id = candidates[0].id

    result = await eng.run_engineering_loop(
        tenant,
        candidate_id,
        "manager-1",
        "manager",
        candidate_repo=cand_repo,
        skill_repo=skill_repo,
        policy_repo=pol_repo,
    )
    assert result.passed is True
    assert result.lifecycle == "REVIEW"

    # The publish gate (unmodified) now finds a passing evaluation ⇒ no 403.
    version = await eng.version_and_publish(
        tenant,
        candidate_id,
        "manager-1",
        "manager",
        candidate_repo=cand_repo,
        skill_repo=skill_repo,
        policy_repo=pol_repo,
    )

    # semver-valid first release, and the eval_score is the loop's measured score.
    parts = version.semver.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts)
    assert version.semver == "0.1.0"
    assert version.approved_by == "manager-1"
    assert version.eval_score is not None

    stored = await cand_repo.get_candidate(tenant, candidate_id)
    assert stored.status == "promoted"
    assert eng.derive_lifecycle(None, await skill_repo.get_skill(tenant, version.skill_id)) == (
        "PUBLISHED"
    )
