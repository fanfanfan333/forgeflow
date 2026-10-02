"""INC43 T03 (Phase 1) — Skill Engineering loop + BE-5 tenant scope.

Covers the new backend modules: ``tenant_scope`` (BE-5), ``contracts``,
``critic``, ``tester``, ``revision`` and ``engineering`` (orchestration +
lifecycle machine). Runs on the **memory** profile only (``STORAGE_BACKEND=memory``)
— no PostgreSQL, no network, no LLM.

Counterfactual-injection discipline: the four load-bearing nails below are
written to go **red** when the guard is loosened, and the loosening is applied
by ``qa_tmp/inc43_t03_inject.py`` (a pytest plugin). For that to work the tests
must reach the guard through the *module attribute* (``tenant_scope.scope_filter``
/ ``engineering.can_transition`` / ``critic.allowed_tool_set``) rather than a
bound import — so the injected override is what actually runs.
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
from forgeflow.skills import contracts as contracts_mod
from forgeflow.skills import critic as critic_mod
from forgeflow.skills import engineering as eng
from forgeflow.skills import revision as revision_mod
from forgeflow.skills import tenant_scope
from forgeflow.skills import tester as tester_mod
from forgeflow.skills.errors import GovernanceError, InsufficientExperiencesError
from forgeflow.skills.models import (
    CANDIDATE_STATUSES,
    SKILL_STATUSES,
    SkillCandidateRecord,
    SkillEvaluationRecord,
)

pytestmark = pytest.mark.asyncio

_WHITELISTED_TOOLS = ["data.query", "analysis.score", "report.render"]
_COMPLETE_DRAFT = {
    "prompt": "分析客户流失并输出挽留建议",
    "steps": ["拉取客户行为数据", "计算流失概率", "生成挽留建议"],
    "tools": list(_WHITELISTED_TOOLS),
    "io_schema": {"input": {"intent": "string"}, "output": {"summary": "string"}},
    "applicable_when": {"domain": "数据分析"},
}


def _tenant() -> str:
    return f"t-inc43-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _clean_store():
    """Isolate the module-global memory skill store per test."""
    clear_skill_store()
    yield
    clear_skill_store()


def _contract(**overrides) -> contracts_mod.SkillContract:
    base = dict(
        goal="分析客户流失",
        procedure=["拉取数据", "计算概率", "生成建议"],
        tools=list(_WHITELISTED_TOOLS),
        inputs={"intent": "string"},
        outputs={"summary": "string"},
        verification=["输出非空"],
        risk_level="low",
    )
    base.update(overrides)
    return contracts_mod.SkillContract(**base)


async def _save_candidate(
    repo: MemorySkillCandidateRepository, tenant: str, draft_spec: dict
) -> SkillCandidateRecord:
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
# BE-5 — tenant scope (require_tenant / scope_filter)                          #
# --------------------------------------------------------------------------- #


async def test_require_tenant_rejects_empty_and_none():
    for bad in (None, ""):
        with pytest.raises(GovernanceError) as excinfo:
            tenant_scope.require_tenant(bad)
        assert excinfo.value.status_code == 403
    assert tenant_scope.require_tenant("t-x") == "t-x"


async def test_scope_filter_is_strict_and_none_never_matches():
    # Nail: a None record tenant matches no tenant.
    assert tenant_scope.scope_filter(None, "t-x") is False
    assert tenant_scope.scope_filter("t-x", "t-x") is True
    assert tenant_scope.scope_filter("t-y", "t-x") is False


async def test_engineering_entry_requires_tenant():
    with pytest.raises(GovernanceError) as excinfo:
        await eng.synthesize(None, mode="auto")
    assert excinfo.value.status_code == 403
    with pytest.raises(GovernanceError) as excinfo2:
        await eng.run_engineering_loop(None, "c-1", "actor")
    assert excinfo2.value.status_code == 403


# --------------------------------------------------------------------------- #
# BE-4 — lifecycle state machine (legal table, illegal ⇒ 403)                  #
# --------------------------------------------------------------------------- #


async def test_illegal_transition_draft_to_published_is_403():
    # Nail: DRAFT → PUBLISHED is illegal (must pass through TESTING/REVIEW).
    assert eng.can_transition("DRAFT", "PUBLISHED") is False
    with pytest.raises(GovernanceError) as excinfo:
        eng.assert_transition("DRAFT", "PUBLISHED")
    assert excinfo.value.status_code == 403


async def test_legal_transitions_from_the_design_table():
    legal = [
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
    ]
    for from_state, to_state in legal:
        assert eng.can_transition(from_state, to_state) is True, (from_state, to_state)
    # A couple of illegal ones beyond DRAFT→PUBLISHED.
    assert eng.can_transition("PUBLISHED", "DRAFT") is False
    assert eng.can_transition("TESTING", "PUBLISHED") is False


async def test_transition_requires_approval_flags_match_the_table():
    assert eng.transition_requires_approval("REVIEW", "PUBLISHED") is True
    assert eng.transition_requires_approval("REVIEW", "REVIEW") is True
    assert eng.transition_requires_approval("PUBLISHED", "DEPRECATED") is True
    assert eng.transition_requires_approval("DRAFT", "CANDIDATE") is False
    assert eng.transition_requires_approval("TESTING", "REVIEW") is False
    with pytest.raises(GovernanceError):
        eng.transition_requires_approval("DRAFT", "PUBLISHED")


async def test_derive_lifecycle_read_only_view():
    tenant = _tenant()
    complete = _contract()
    # candidate.draft + complete contract ⇒ CANDIDATE
    cand_draft = SkillCandidateRecord(tenant_id=tenant, status="draft")
    assert eng.derive_lifecycle(cand_draft, None, contract=complete) == "CANDIDATE"
    assert eng.derive_lifecycle(cand_draft, None) == "DRAFT"
    for status, expected in (
        ("promoted", "PUBLISHED"),
        ("approved", "REVIEW"),
        ("evaluating", "TESTING"),
        ("rejected", "DRAFT"),
        ("insufficient", "DRAFT"),
    ):
        cand = SkillCandidateRecord(tenant_id=tenant, status=status)
        assert eng.derive_lifecycle(cand, None) == expected
    from forgeflow.skills.models import SkillRecord

    assert eng.derive_lifecycle(None, SkillRecord(status="published")) == "PUBLISHED"
    assert eng.derive_lifecycle(None, SkillRecord(status="retired")) == "DEPRECATED"


async def test_existing_status_enums_are_unchanged():
    # Red line: the stored enums must not change value.
    assert SKILL_STATUSES == ("draft", "evaluating", "published", "retired")
    assert CANDIDATE_STATUSES == (
        "draft",
        "evaluating",
        "approved",
        "rejected",
        "insufficient",
        "promoted",
    )


# --------------------------------------------------------------------------- #
# contracts — lift / project                                                  #
# --------------------------------------------------------------------------- #


async def test_contract_from_draft_spec_and_back():
    contract = contracts_mod.SkillContract.from_draft_spec(_COMPLETE_DRAFT)
    assert contract.goal == _COMPLETE_DRAFT["prompt"]
    assert contract.procedure == _COMPLETE_DRAFT["steps"]
    assert contract.tools == _WHITELISTED_TOOLS
    assert contract.inputs == {"intent": "string"}
    assert contract.outputs == {"summary": "string"}
    assert contract.is_complete() is True
    # round-trip preserves the compiler's four elements
    spec = contract.to_draft_spec()
    assert spec["prompt"] == _COMPLETE_DRAFT["prompt"]
    assert spec["steps"] == _COMPLETE_DRAFT["steps"]
    assert spec["tools"] == _WHITELISTED_TOOLS
    assert spec["io_schema"]["input"] == {"intent": "string"}


async def test_contract_is_complete_false_when_element_missing():
    assert _contract(goal="").is_complete() is False
    assert _contract(procedure=[]).is_complete() is False
    assert _contract(tools=[]).is_complete() is False
    assert _contract(inputs={}, outputs={}).is_complete() is False


# --------------------------------------------------------------------------- #
# critic — deterministic, fail-closed                                          #
# --------------------------------------------------------------------------- #


async def test_critic_clean_on_complete_contract():
    crit = critic_mod.critique(_contract())
    assert crit.must_fix == []
    assert crit.severity in ("none", "low", "medium")


async def test_critic_flags_off_whitelist_tool():
    # Nail: an off-whitelist tool is a high finding ⇒ must_fix.
    bad = _contract(tools=["data.query", "evil.exfiltrate"])
    crit = critic_mod.critique(bad)
    assert "tools_not_whitelisted" in crit.must_fix
    codes = {f["code"] for f in crit.findings}
    assert "tools_not_whitelisted" in codes


async def test_critic_flags_missing_core_elements():
    crit = critic_mod.critique(_contract(goal="", procedure=[], tools=[]))
    assert set(crit.must_fix) >= {"goal_missing", "procedure_missing", "tools_missing"}
    assert crit.severity == "high"


async def test_critic_flags_high_risk_without_verification():
    crit = critic_mod.critique(_contract(risk_level="high", verification=[]))
    assert "high_risk_unverified" in crit.must_fix


# --------------------------------------------------------------------------- #
# tester — offline sandbox (pass / fail / error)                               #
# --------------------------------------------------------------------------- #


async def test_generate_tests_covers_four_categories():
    cases = tester_mod.generate_tests(_contract())
    categories = {c.category for c in cases}
    assert categories == {"normal", "boundary", "adversarial", "security"}
    assert all(c.assertion for c in cases)
    assert all(isinstance(c.input, dict) for c in cases)


async def test_run_tests_deterministic_pass_fail_error():
    contract = _contract(verification=[])  # boundary case must fail
    cases = tester_mod.generate_tests(contract)
    runs = tester_mod.run_tests(contract, cases)
    assert len(runs) == len(cases)
    by_id = {r.case_id: r for r in runs}
    assert by_id["case-boundary-verification"].verdict == "fail"
    assert by_id["case-adversarial-tools"].verdict == "pass"
    # Determinism: a second run is identical.
    runs2 = tester_mod.run_tests(contract, cases)
    assert [r.verdict for r in runs2] == [r.verdict for r in runs]


async def test_run_tests_error_is_never_pass():
    contract = _contract()
    cases = [
        contracts_mod.SkillTestCase(
            id="case-unknown", category="normal", input={}, assertion="nope"
        ),
        contracts_mod.SkillTestCase(
            id="case-bad-input", category="normal", input=["not", "a", "dict"], assertion="input_keys_declared"
        ),
    ]
    runs = tester_mod.run_tests(contract, cases)
    assert {r.verdict for r in runs} == {"error"}
    assert all(r.verdict != "pass" for r in runs)


async def test_structural_score_uses_evaluator_rubric():
    # A complete, 3-tool contract reaches the evaluator's pass threshold.
    assert tester_mod.structural_score(_contract()) >= 0.6
    # An empty contract scores well below it.
    assert tester_mod.structural_score(contracts_mod.SkillContract()) < 0.6


# --------------------------------------------------------------------------- #
# sandbox_evaluate — pass_rate vs verified_pass_rate(excludes error)           #
# --------------------------------------------------------------------------- #


async def test_sandbox_evaluate_rates_exclude_error_from_denominator():
    contract = _contract(verification=[])  # verification_declared ⇒ fail
    cases = [
        contracts_mod.SkillTestCase(id="c1", category="normal", input={}, assertion="input_keys_declared"),
        contracts_mod.SkillTestCase(id="c2", category="normal", input={}, assertion="tools_whitelisted"),
        contracts_mod.SkillTestCase(id="c3", category="boundary", input={}, assertion="verification_declared"),
        contracts_mod.SkillTestCase(id="c4", category="normal", input={}, assertion="unknown_assertion"),
    ]
    evaluation = eng.sandbox_evaluate(contract, cases)
    assert evaluation.sample_size == 4
    # pass_rate denominator = total (4) → 2/4
    assert evaluation.pass_rate == 0.5
    # verified_pass_rate denominator excludes the 1 error → 2/3
    assert evaluation.verified_pass_rate == round(2 / 3, 4)
    assert evaluation.failure_modes == ["verification_declared"]
    assert evaluation.ran_at  # non-empty ISO timestamp


async def test_sandbox_evaluate_no_vacuous_pass_on_all_error():
    contract = _contract()
    cases = [
        contracts_mod.SkillTestCase(id="e1", category="normal", input={}, assertion="nope"),
        contracts_mod.SkillTestCase(id="e2", category="normal", input={}, assertion="also-nope"),
    ]
    evaluation = eng.sandbox_evaluate(contract, cases)
    assert evaluation.pass_rate == 0.0
    assert evaluation.verified_pass_rate == 0.0


# --------------------------------------------------------------------------- #
# revision — deterministic repair, semver not overwritten                      #
# --------------------------------------------------------------------------- #


async def test_repair_clears_off_whitelist_tools_and_proposes_semver():
    original = _contract(tools=["data.query", "evil.exfiltrate"])
    crit = critic_mod.critique(original)
    repaired, revision = revision_mod.repair(original, crit, None, round=1)
    # illegal tool removed; repair did not touch the original object
    assert "evil.exfiltrate" not in repaired.tools
    assert "evil.exfiltrate" in original.tools
    assert revision.round == 1
    assert revision.from_semver == "0.1.0"
    assert revision.to_semver == "0.1.1"
    assert "tools_added" in revision.diff_summary
    assert "tools_removed" in revision.diff_summary
    assert revision.diff_summary["tools_removed"] == ["evil.exfiltrate"]


async def test_repair_is_deterministic_and_bumps_semver_per_round():
    original = _contract(goal="", procedure=[])
    crit = critic_mod.critique(original)
    r1, rev1 = revision_mod.repair(original, crit, None, round=1)
    r1b, rev1b = revision_mod.repair(original, crit, None, round=1)
    assert r1.to_dict() == r1b.to_dict()  # deterministic
    assert rev1.to_semver == rev1b.to_semver == "0.1.1"
    assert r1.goal and r1.procedure  # gaps filled
    _, rev2 = revision_mod.repair(original, crit, None, round=2)
    assert rev2.from_semver == "0.1.1"
    assert rev2.to_semver == "0.1.2"


async def test_repair_rounds_are_bounded_by_loop_max_repair():
    # The loop caps repair at max_repair; verify the wiring via a tiny cap.
    tenant = _tenant()
    cand_repo = MemorySkillCandidateRepository()
    skill_repo = MemorySkillRepository()
    pol_repo = MemoryPolicyRepository()
    await _save_candidate(cand_repo, tenant, {})  # empty ⇒ every gap missing
    result = await eng.run_engineering_loop(
        tenant,
        (await cand_repo.list_candidates(tenant))[0].id,
        "manager-1",
        "manager",
        max_repair=0,
        candidate_repo=cand_repo,
        skill_repo=skill_repo,
        policy_repo=pol_repo,
    )
    assert result.rounds == 0
    assert result.passed is False
    assert result.lifecycle == "DRAFT"
    assert result.degraded_reason


# --------------------------------------------------------------------------- #
# engineering — synthesize + full loop + version_and_publish                   #
# --------------------------------------------------------------------------- #


async def _seed_experiences(repo: MemoryExperienceRepository, tenant: str, n: int):
    text = "分析销售数据 数据分析"
    embedding = deterministic_embedding(text)
    recs = []
    for i in range(n):
        rec = ExperienceRecord(
            tenant_id=tenant,
            run_id=f"r-{i}",
            summary=f"{text} — 共执行 2 个步骤，结果：success",
            outcome="success",
            reusable_steps=[{"tool": "data.query"}, {"tool": "report.render"}],
            tags=["数据分析"],
            embedding=embedding,
        )
        await repo.save(rec)
        recs.append(rec)
    return recs


async def test_synthesize_lifts_candidate_into_whitelisted_contract():
    tenant = _tenant()
    exp_repo = MemoryExperienceRepository()
    cand_repo = MemorySkillCandidateRepository()
    await _seed_experiences(exp_repo, tenant, 3)

    contract = await eng.synthesize(
        tenant, mode="auto", candidate_repo=cand_repo, experience_repo=exp_repo
    )

    assert contract.is_complete() is True
    assert contract.procedure and contract.inputs and contract.outputs
    # every tool is a real platform tool (⊆ allowed_tool_set)
    from forgeflow.skills.trust_baseline import allowed_tool_set

    assert set(contract.tools) <= set(allowed_tool_set())


async def test_synthesize_raises_on_insufficient_experiences():
    tenant = _tenant()
    exp_repo = MemoryExperienceRepository()
    cand_repo = MemorySkillCandidateRepository()
    await _seed_experiences(exp_repo, tenant, 1)
    with pytest.raises(InsufficientExperiencesError):
        await eng.synthesize(
            tenant, mode="auto", candidate_repo=cand_repo, experience_repo=exp_repo
        )


async def test_run_engineering_loop_closes_to_review():
    tenant = _tenant()
    cand_repo = MemorySkillCandidateRepository()
    skill_repo = MemorySkillRepository()
    pol_repo = MemoryPolicyRepository()
    candidate = await _save_candidate(cand_repo, tenant, {})  # empty ⇒ must repair

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
    assert result.lifecycle == "REVIEW"
    assert result.rounds >= 1  # at least one repair round was needed
    assert result.revisions  # SkillRevision records exist
    assert result.test_cases and result.test_runs
    assert result.evaluation.pass_rate >= eng.TEST_PASS_THRESHOLD


async def test_run_engineering_loop_missing_candidate_is_404():
    tenant = _tenant()
    cand_repo = MemorySkillCandidateRepository()
    with pytest.raises(GovernanceError) as excinfo:
        await eng.run_engineering_loop(
            tenant, "does-not-exist", "manager-1", candidate_repo=cand_repo
        )
    assert excinfo.value.status_code == 404


async def test_version_and_publish_delegates_to_governance_gate():
    tenant = _tenant()
    cand_repo = MemorySkillCandidateRepository()
    skill_repo = MemorySkillRepository()
    pol_repo = MemoryPolicyRepository()
    candidate = await _save_candidate(cand_repo, tenant, _COMPLETE_DRAFT)
    await cand_repo.save_evaluation(
        SkillEvaluationRecord(
            tenant_id=tenant,
            target_id=candidate.id,
            metrics={"score": 0.9},
            verdict="pass",
        )
    )

    version = await eng.version_and_publish(
        tenant,
        candidate.id,
        "manager-1",
        "manager",
        candidate_repo=cand_repo,
        skill_repo=skill_repo,
        policy_repo=pol_repo,
    )

    assert version.semver == "0.1.0"
    assert version.approved_by == "manager-1"
    assert (await cand_repo.get_candidate(tenant, candidate.id)).status == "promoted"
