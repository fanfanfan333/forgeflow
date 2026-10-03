"""INC44 T01 — Skill engineering loop → publish closure (the P1 defect fix).

Why this file exists
--------------------
Before INC44 ``run_engineering_loop`` was a **pure computation**: it returned a
``SkillEngineeringResult`` but never persisted the evaluation it had actually run
nor advanced the candidate's status. The publish gate
(``governance_gate.promote_candidate``) requires a stored ``verdict == "pass"``
evaluation, so "run the loop → publish" always 403'd with "candidate has no
evaluation".

The fix is a small additive side effect on the loop's **three exits** — and this
file pins each of them (design §1.1 / §7 T01):

  1. success  ⇒ ``SkillEvaluationRecord(verdict="pass")`` persisted, ``metrics``
     keyed on ``tester.structural_score`` (one yardstick with the evaluator), the
     **repaired** contract written back onto ``candidate.draft_spec`` and the
     candidate advanced to ``approved`` (⇔ the ``REVIEW`` state);
  2. exhaustion ⇒ ``SkillEvaluationRecord(verdict="fail")`` persisted with the
     status left **unchanged** (an honest downgrade) so a later ``promote`` still
     403s;
  3. critique-blocked ⇒ **nothing** is persisted (no evaluation ⇒ not publishable).

Counterfactual discipline: remove the persist call and test (1)/(2) go red;
loosen ``_tests_passed`` and (2) goes red.

Runs on the **memory** profile only (``STORAGE_BACKEND=memory``) — no PostgreSQL,
no network, no LLM.
"""

from __future__ import annotations

import uuid

import pytest

from forgeflow.repositories.memory.policy_repo import MemoryPolicyRepository
from forgeflow.repositories.memory.skill_repo import (
    MemorySkillCandidateRepository,
    MemorySkillRepository,
    clear_skill_store,
)
from forgeflow.skills import engineering as eng
from forgeflow.skills.errors import GovernanceError
from forgeflow.skills.models import (
    CANDIDATE_STATUSES,
    SKILL_STATUSES,
    SkillCandidateRecord,
)
from forgeflow.skills.tester import structural_score

pytestmark = pytest.mark.asyncio

#: A complete draft (four gating elements present, tools all whitelisted) ⇒ the
#: critique is clean and the sandbox passes (4/5 ≥ 0.6).
_COMPLETE_DRAFT = {
    "prompt": "分析客户流失并输出挽留建议",
    "steps": ["拉取客户行为数据", "计算流失概率", "生成挽留建议"],
    "tools": ["data.query", "analysis.score", "report.render"],
    "io_schema": {"input": {"intent": "string"}, "output": {"summary": "string"}},
    "applicable_when": {"domain": "数据分析"},
}

#: A draft that is critique-clean but whose sandbox **fails** (only 1 tool ⇒
#: structural score 0.5667 < 0.6; no io ⇒ 2/5 cases pass). With ``max_repair=0``
#: this reaches the exhaustion exit without any repair round.
_EXHAUSTING_DRAFT = {
    "prompt": "一句话目标",
    "steps": ["唯一步骤"],
    "tools": ["report.render"],
    "applicable_when": {},
}


def _tenant() -> str:
    return f"t-inc44-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _clean_store():
    clear_skill_store()
    yield
    clear_skill_store()


async def _save_candidate(repo, tenant: str, draft_spec: dict) -> SkillCandidateRecord:
    candidate = SkillCandidateRecord(
        tenant_id=tenant,
        name=f"候选 {uuid.uuid4().hex[:6]}",
        domain="数据分析",
        experience_ids=["e1", "e2", "e3"],
        draft_spec=dict(draft_spec),
        status="draft",
    )
    await repo.save_candidate(candidate)
    return candidate


# --------------------------------------------------------------------------- #
# 1. Success exit — persist + write-back + advance to REVIEW                    #
# --------------------------------------------------------------------------- #
async def test_success_loop_persists_a_passing_evaluation():
    """Criterion ①: a non-empty, ``pass`` evaluation keyed on the structural score."""
    tenant = _tenant()
    cand_repo = MemorySkillCandidateRepository()
    skill_repo = MemorySkillRepository()
    pol_repo = MemoryPolicyRepository()
    candidate = await _save_candidate(cand_repo, tenant, _COMPLETE_DRAFT)

    result = await eng.run_engineering_loop(
        tenant,
        candidate.id,
        "manager-1",
        "manager",
        candidate_repo=cand_repo,
        skill_repo=skill_repo,
        policy_repo=pol_repo,
    )

    assert result.passed is True
    evaluation = await cand_repo.get_evaluation_for(tenant, candidate.id)
    assert evaluation is not None, "the loop must persist the evaluation it ran"
    assert evaluation.verdict == "pass"
    # One yardstick: the persisted score IS tester.structural_score(contract).
    assert evaluation.metrics["score"] == structural_score(result.contract)
    # The sandbox facts ride along for traceability (never fabricated).
    assert evaluation.metrics["sample_size"] == result.evaluation.sample_size
    assert evaluation.metrics["pass_rate"] == result.evaluation.pass_rate


async def test_success_loop_advances_candidate_to_review_and_writes_back_the_contract():
    """Criteria ②/③: ``approved`` ⇔ ``REVIEW``, and the published spec is verified."""
    tenant = _tenant()
    cand_repo = MemorySkillCandidateRepository()
    candidate = await _save_candidate(cand_repo, tenant, _COMPLETE_DRAFT)

    result = await eng.run_engineering_loop(
        tenant,
        candidate.id,
        "manager-1",
        "manager",
        candidate_repo=cand_repo,
        skill_repo=MemorySkillRepository(),
        policy_repo=MemoryPolicyRepository(),
    )

    stored = await cand_repo.get_candidate(tenant, candidate.id)
    assert stored is not None
    assert stored.status == "approved"
    assert eng.derive_lifecycle(stored) == "REVIEW"
    # The returned state and the derived state agree (the INC43 inconsistency).
    assert result.lifecycle == "REVIEW"
    # The verified contract is exactly what will be published.
    assert stored.draft_spec == result.contract.to_draft_spec()


# --------------------------------------------------------------------------- #
# 2. Exhaustion exit — honest ``fail``, status unchanged, promote still 403     #
# --------------------------------------------------------------------------- #
async def test_exhaustion_persists_a_failing_evaluation_and_leaves_status():
    """Criterion ④: ``fail`` persisted; the candidate status is NOT advanced."""
    tenant = _tenant()
    cand_repo = MemorySkillCandidateRepository()
    candidate = await _save_candidate(cand_repo, tenant, _EXHAUSTING_DRAFT)

    result = await eng.run_engineering_loop(
        tenant,
        candidate.id,
        "manager-1",
        "manager",
        max_repair=0,
        candidate_repo=cand_repo,
        skill_repo=MemorySkillRepository(),
        policy_repo=MemoryPolicyRepository(),
    )

    assert result.passed is False
    assert result.lifecycle == "DRAFT"
    assert result.degraded_reason
    evaluation = await cand_repo.get_evaluation_for(tenant, candidate.id)
    assert evaluation is not None
    assert evaluation.verdict == "fail"
    stored = await cand_repo.get_candidate(tenant, candidate.id)
    assert stored.status == "draft"  # honest downgrade, not silently advanced

    # And a later promote is still refused (the stored verdict is ``fail``).
    with pytest.raises(GovernanceError):
        await eng.version_and_publish(
            tenant,
            candidate.id,
            "manager-1",
            "manager",
            candidate_repo=cand_repo,
            skill_repo=MemorySkillRepository(),
            policy_repo=MemoryPolicyRepository(),
        )


async def test_exhaustion_status_unchanged_is_distinct_from_success():
    """Contrast pin: the same contract shape succeeds once the io is declared."""
    tenant = _tenant()
    cand_repo = MemorySkillCandidateRepository()
    # Add io_schema ⇒ the structural score clears the bar and the loop closes.
    fixed = dict(_EXHAUSTING_DRAFT)
    fixed["tools"] = ["report.render", "data.query", "analysis.score"]
    fixed["io_schema"] = {"input": {"intent": "string"}, "output": {"summary": "string"}}
    candidate = await _save_candidate(cand_repo, tenant, fixed)

    result = await eng.run_engineering_loop(
        tenant,
        candidate.id,
        "manager-1",
        "manager",
        max_repair=0,
        candidate_repo=cand_repo,
        skill_repo=MemorySkillRepository(),
        policy_repo=MemoryPolicyRepository(),
    )
    assert result.passed is True
    assert (await cand_repo.get_candidate(tenant, candidate.id)).status == "approved"


# --------------------------------------------------------------------------- #
# 3. Critique-blocked exit — nothing persisted                                  #
# --------------------------------------------------------------------------- #
async def test_critique_blocked_persists_nothing():
    """Criterion ⑤: a blocker that survives ⇒ no evaluation is stored."""
    tenant = _tenant()
    cand_repo = MemorySkillCandidateRepository()
    # An empty draft ⇒ critique has must_fix; max_repair=0 ⇒ no repair round.
    candidate = await _save_candidate(cand_repo, tenant, {})

    result = await eng.run_engineering_loop(
        tenant,
        candidate.id,
        "manager-1",
        "manager",
        max_repair=0,
        candidate_repo=cand_repo,
        skill_repo=MemorySkillRepository(),
        policy_repo=MemoryPolicyRepository(),
    )

    assert result.passed is False
    assert result.lifecycle == "DRAFT"
    assert result.critique.must_fix  # a real blocker was reported
    assert await cand_repo.get_evaluation_for(tenant, candidate.id) is None
    assert (await cand_repo.get_candidate(tenant, candidate.id)).status == "draft"
    # And there is nothing to promote.
    with pytest.raises(GovernanceError):
        await eng.version_and_publish(
            tenant,
            candidate.id,
            "manager-1",
            "manager",
            candidate_repo=cand_repo,
            skill_repo=MemorySkillRepository(),
            policy_repo=MemoryPolicyRepository(),
        )


# --------------------------------------------------------------------------- #
# 4. Tenant isolation + red lines                                              #
# --------------------------------------------------------------------------- #
async def test_loop_and_publish_require_a_tenant():
    """Criterion ⑦: an unresolved tenant is a 403 *before* any read (BE-5)."""
    for bad in (None, ""):
        with pytest.raises(GovernanceError) as excinfo:
            await eng.run_engineering_loop(bad, "c-1", "actor")
        assert excinfo.value.status_code == 403
        with pytest.raises(GovernanceError) as excinfo2:
            await eng.version_and_publish(bad, "c-1", "actor")
        assert excinfo2.value.status_code == 403


async def test_lifecycle_table_and_status_enums_are_unchanged():
    """Criterion ⑦: the six-state machine and the stored enums are untouched."""
    # The full legal transition table (design §3.3), verbatim.
    assert set(eng.LIFECYCLE_TRANSITIONS) == {
        ("DRAFT", "CANDIDATE"),
        ("DRAFT", "DRAFT"),
        ("CANDIDATE", "TESTING"),
        ("CANDIDATE", "DRAFT"),
        ("TESTING", "CANDIDATE"),
        ("TESTING", "REVIEW"),
        ("TESTING", "DRAFT"),
        ("REVIEW", "PUBLISHED"),
        ("REVIEW", "REVIEW"),
        ("REVIEW", "CANDIDATE"),
        ("PUBLISHED", "DEPRECATED"),
        ("PUBLISHED", "CANDIDATE"),
        ("DEPRECATED", "PUBLISHED"),
    }
    assert SKILL_STATUSES == ("draft", "evaluating", "published", "retired")
    assert CANDIDATE_STATUSES == (
        "draft",
        "evaluating",
        "approved",
        "rejected",
        "insufficient",
        "promoted",
    )
