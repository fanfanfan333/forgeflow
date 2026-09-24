"""Skill release gate — an upgrade must not regress against what it supersedes.

Covers the three layers:
  * ``evaluate_release`` as a pure decision function (severity ladder),
  * ``baseline_from_version`` — what the *stored* data can honestly prove,
  * and the wiring in ``promote_candidate``, including that a blocked promotion
    leaves the skill's published version untouched.
"""

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
from forgeflow.skills.models import (
    SkillCandidateRecord,
    SkillEvaluationRecord,
    SkillVersionRecord,
)
from forgeflow.skills.release_gate import (
    RELEASE_TOLERANCES,
    baseline_from_version,
    evaluate_release,
)

_DRAFT = {
    "prompt": "分析客户流失",
    "steps": ["拉取数据", "计算概率"],
    "tools": ["data.query", "analysis.score", "report.render"],
    "io_schema": {"input": {"intent": "string"}, "output": {"summary": "string"}},
}


# --------------------------------------------------------------------------- #
# Policy — evaluate_release (pure)                                             #
# --------------------------------------------------------------------------- #

def test_no_baseline_is_allowed_but_labelled():
    decision = evaluate_release({"score": 0.9}, None)
    assert decision.allowed is True
    assert decision.severity == "no_baseline"
    assert decision.baseline_present is False


def test_empty_baseline_is_treated_as_no_baseline():
    assert evaluate_release({"score": 0.9}, {}).severity == "no_baseline"


def test_score_regression_blocks_the_release():
    decision = evaluate_release({"score": 0.60}, {"score": 0.90})
    assert decision.allowed is False
    assert decision.severity == "regression"
    assert any(f.metric == "score" for f in decision.findings)
    assert "score" in decision.reason


def test_score_slip_inside_fail_tolerance_is_a_warning_and_still_allowed():
    # fail tolerance for score is 0.05; a 0.03 slip is a warning only.
    decision = evaluate_release({"score": 0.87}, {"score": 0.90})
    assert decision.allowed is True
    assert decision.severity == "warning"


def test_equal_or_better_score_is_ok():
    assert evaluate_release({"score": 0.90}, {"score": 0.90}).severity == "ok"
    assert evaluate_release({"score": 0.95}, {"score": 0.90}).severity == "ok"


def test_release_table_extends_the_agent_eval_table():
    """One tolerance policy for the platform — plus the skill's own score."""
    from forgeflow.evaluation.regression import TOLERANCES

    for metric in TOLERANCES:
        assert metric in RELEASE_TOLERANCES
    assert "score" in RELEASE_TOLERANCES


def test_lower_is_better_metrics_are_compared_too():
    """A hallucination-rate blow-up is a regression even if score holds."""
    decision = evaluate_release(
        {"score": 0.90, "hallucination_rate": 0.30},
        {"score": 0.90, "hallucination_rate": 0.05},
    )
    assert decision.allowed is False
    assert decision.severity == "regression"


def test_metrics_absent_from_either_side_are_skipped():
    """No guessing: an unrecorded dimension is not compared."""
    decision = evaluate_release({"score": 0.9}, {"score": 0.9, "avg_latency_ms": 100.0})
    assert decision.severity == "ok"


def test_decision_is_json_serialisable():
    payload = evaluate_release({"score": 0.6}, {"score": 0.9}).to_dict()
    assert payload["allowed"] is False
    assert payload["findings"][0]["metric"] == "score"


# --------------------------------------------------------------------------- #
# Baseline extraction                                                          #
# --------------------------------------------------------------------------- #

def test_baseline_from_missing_version_is_empty():
    assert baseline_from_version(None) == {}


def test_baseline_from_a_version_without_a_score_is_empty():
    """An unevaluated version must not fabricate a perfect baseline of 1.0."""
    assert baseline_from_version(SkillVersionRecord(semver="0.1.0")) == {}


def test_baseline_reads_the_recorded_eval_score():
    assert baseline_from_version(SkillVersionRecord(semver="0.1.0", eval_score=0.87)) == {
        "score": 0.87
    }


# --------------------------------------------------------------------------- #
# Wiring — promote_candidate                                                    #
# --------------------------------------------------------------------------- #

async def _repos():
    return (
        MemorySkillCandidateRepository(),
        MemorySkillRepository(),
        MemoryPolicyRepository(),
    )


async def _candidate(
    repo: MemorySkillCandidateRepository, tenant: str, name: str
) -> SkillCandidateRecord:
    candidate = SkillCandidateRecord(
        tenant_id=tenant,
        name=name,
        domain="数据分析",
        experience_ids=["e1", "e2", "e3"],
        draft_spec=dict(_DRAFT),
        status="draft",
    )
    await repo.save_candidate(candidate)
    return candidate


async def _evaluate(repo, tenant: str, candidate_id: str, score: float) -> None:
    await repo.save_evaluation(
        SkillEvaluationRecord(
            tenant_id=tenant, target_id=candidate_id, metrics={"score": score}, verdict="pass"
        )
    )


async def _promote(cand_repo, skill_repo, pol_repo, tenant: str, candidate_id: str):
    return await promote_candidate(
        candidate_id,
        "manager-1",
        tenant_id=tenant,
        candidate_repo=cand_repo,
        skill_repo=skill_repo,
        policy_repo=pol_repo,
        actor_role="manager",
    )


@pytest.mark.asyncio
async def test_first_release_has_no_baseline_and_promotes():
    tenant = f"t-rel-{uuid.uuid4().hex[:8]}"
    cand_repo, skill_repo, pol_repo = await _repos()
    name = f"发布门禁技能-{uuid.uuid4().hex[:6]}"
    candidate = await _candidate(cand_repo, tenant, name)
    await _evaluate(cand_repo, tenant, candidate.id, 0.9)

    version = await _promote(cand_repo, skill_repo, pol_repo, tenant, candidate.id)

    assert version.semver == "0.1.0"
    assert version.eval_score == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_regressing_upgrade_is_blocked_and_version_is_untouched():
    tenant = f"t-rel-{uuid.uuid4().hex[:8]}"
    cand_repo, skill_repo, pol_repo = await _repos()
    name = f"回归技能-{uuid.uuid4().hex[:6]}"

    first = await _candidate(cand_repo, tenant, name)
    await _evaluate(cand_repo, tenant, first.id, 0.9)
    await _promote(cand_repo, skill_repo, pol_repo, tenant, first.id)

    skill = await skill_repo.get_skill_by_name(tenant, name)
    assert skill is not None and skill.current_version == "0.1.0"

    second = await _candidate(cand_repo, tenant, name)
    await _evaluate(cand_repo, tenant, second.id, 0.60)

    with pytest.raises(GovernanceError) as excinfo:
        await _promote(cand_repo, skill_repo, pol_repo, tenant, second.id)

    assert excinfo.value.status_code == 403
    assert "发布门禁未通过" in str(excinfo.value)

    # Nothing was published behind the gate's back.
    versions = await skill_repo.list_versions(tenant, skill.id)
    assert [v.semver for v in versions] == ["0.1.0"]
    assert (await skill_repo.get_skill_by_name(tenant, name)).current_version == "0.1.0"


@pytest.mark.asyncio
async def test_non_regressing_upgrade_publishes_a_new_version():
    tenant = f"t-rel-{uuid.uuid4().hex[:8]}"
    cand_repo, skill_repo, pol_repo = await _repos()
    name = f"演进技能-{uuid.uuid4().hex[:6]}"

    first = await _candidate(cand_repo, tenant, name)
    await _evaluate(cand_repo, tenant, first.id, 0.80)
    await _promote(cand_repo, skill_repo, pol_repo, tenant, first.id)

    second = await _candidate(cand_repo, tenant, name)
    await _evaluate(cand_repo, tenant, second.id, 0.95)
    version = await _promote(cand_repo, skill_repo, pol_repo, tenant, second.id)

    assert version.semver == "0.2.0"
    assert version.eval_score == pytest.approx(0.95)


@pytest.mark.asyncio
async def test_gate_can_be_switched_off(monkeypatch):
    """The toggle reproduces the pre-gate behaviour for a regression."""
    from forgeflow.config import get_settings

    monkeypatch.setattr(get_settings(), "skill_release_gate_enabled", False)

    tenant = f"t-rel-{uuid.uuid4().hex[:8]}"
    cand_repo, skill_repo, pol_repo = await _repos()
    name = f"开关技能-{uuid.uuid4().hex[:6]}"

    first = await _candidate(cand_repo, tenant, name)
    await _evaluate(cand_repo, tenant, first.id, 0.9)
    await _promote(cand_repo, skill_repo, pol_repo, tenant, first.id)

    second = await _candidate(cand_repo, tenant, name)
    await _evaluate(cand_repo, tenant, second.id, 0.60)
    version = await _promote(cand_repo, skill_repo, pol_repo, tenant, second.id)

    assert version.semver == "0.2.0"
