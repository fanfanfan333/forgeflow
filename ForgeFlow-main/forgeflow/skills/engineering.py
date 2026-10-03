"""BE-3 / BE-4 — the Skill Engineering orchestration loop + lifecycle machine.

This module is the **orchestration + contract layer** INC43 §S3 asks for. It does
*not* re-implement the loop's building blocks — every step **reuses** the
existing, already-tested machinery:

======================  ====================================================
existing (reused)        new (orchestration)
======================  ====================================================
``candidate_compiler``   :func:`synthesize` — DraftSpec → :class:`SkillContract`
``evaluator`` rubric     :func:`sandbox_evaluate` — case runs → ``SkillEvaluation``
``governance_gate``      :func:`version_and_publish` — promote (RBAC+trust+audit)
``trust_baseline``       single tool-whitelist source of truth (``tools ⊆`` 白名单)
``critic`` / ``tester``  :func:`run_engineering_loop` wiring
``revision``             repair rounds (≤ ``max_repair``), semver never overwritten
======================  ====================================================

The **6-state lifecycle** (``DRAFT → CANDIDATE → TESTING → REVIEW → PUBLISHED →
DEPRECATED``) is a *read-only derivation* over the existing records — it never
adds a new stored enum and never changes ``SKILL_STATUSES`` /
``CANDIDATE_STATUSES`` (INC43 §3.3). An illegal transition (e.g.
``DRAFT → PUBLISHED``) raises ``GovernanceError(403)`` — fail-closed.

BE-5 is enforced on entry: every entry point calls
``tenant_scope.require_tenant`` first, so an unresolved tenant is a 403 *before*
any record is read (the loop cannot be run "for all tenants").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from forgeflow.config import get_settings
from forgeflow.repositories import (
    get_policy_repository,
    get_skill_candidate_repository,
    get_skill_repository,
)
from forgeflow.repositories.base import utcnow
from forgeflow.skills.candidate_compiler import compile_candidate
from forgeflow.skills.contracts import (
    HIGH_RISK,
    SkillContract,
    SkillCritique,
    SkillEvaluation,
    SkillRevision,
    SkillTestCase,
    SkillTestRun,
)
from forgeflow.skills.critic import critique
from forgeflow.skills.errors import GovernanceError, InsufficientExperiencesError
from forgeflow.skills.evaluator import _PASS_THRESHOLD
from forgeflow.skills.governance_gate import promote_candidate
from forgeflow.skills.models import SkillCandidateRecord, SkillVersionRecord
from forgeflow.skills.revision import repair
from forgeflow.skills.tester import generate_tests, run_tests, structural_score
from forgeflow.skills.tenant_scope import require_tenant
from forgeflow.skills.trust_baseline import allowed_tool_set

__all__ = [
    "LIFECYCLE_STATES",
    "LIFECYCLE_TRANSITIONS",
    "LifecycleEdge",
    "TEST_PASS_THRESHOLD",
    "can_transition",
    "assert_transition",
    "transition_requires_approval",
    "derive_lifecycle",
    "SkillEngineeringResult",
    "synthesize",
    "sandbox_evaluate",
    "version_and_publish",
    "run_engineering_loop",
    "critique",
    "generate_tests",
    "repair",
]

# The six read-only lifecycle states (INC43 §3.3).
LIFECYCLE_STATES = ("DRAFT", "CANDIDATE", "TESTING", "REVIEW", "PUBLISHED", "DEPRECATED")

#: TESTING → REVIEW requires ``pass_rate`` to reach this bar. Reuses the
#: evaluator's own threshold so the sandbox and promote share *one* yardstick.
TEST_PASS_THRESHOLD = _PASS_THRESHOLD


@dataclass(frozen=True)
class LifecycleEdge:
    """One legal transition plus its condition + approval requirement."""

    from_state: str
    to_state: str
    condition: str
    requires_approve: bool


#: The complete legal transition table (INC43 §3.3). Any ``(from, to)`` pair
#: absent here is illegal ⇒ 403.
LIFECYCLE_TRANSITIONS: dict[tuple[str, str], LifecycleEdge] = {
    ("DRAFT", "CANDIDATE"): LifecycleEdge(
        "DRAFT", "CANDIDATE", "synthesize() 产出完整契约（四要素齐）", False
    ),
    ("DRAFT", "DRAFT"): LifecycleEdge(
        "DRAFT", "DRAFT", "critique() 有 must_fix（原地补全）", False
    ),
    ("CANDIDATE", "TESTING"): LifecycleEdge(
        "CANDIDATE", "TESTING", "critique().must_fix == []", False
    ),
    ("CANDIDATE", "DRAFT"): LifecycleEdge(
        "CANDIDATE", "DRAFT", "评审否定且不可自动补全", False
    ),
    ("TESTING", "CANDIDATE"): LifecycleEdge(
        "TESTING", "CANDIDATE", "测试未全过 ⇒ 进入 repair(round)", False
    ),
    ("TESTING", "REVIEW"): LifecycleEdge(
        "TESTING", "REVIEW", "测试通过（pass_rate 达阈值）", False
    ),
    ("TESTING", "DRAFT"): LifecycleEdge(
        "TESTING", "DRAFT", "repair 轮次耗尽且仍未过（诚实降级）", False
    ),
    ("REVIEW", "PUBLISHED"): LifecycleEdge(
        "REVIEW", "PUBLISHED", "version_and_publish()（risk != high 且评估 pass）", True
    ),
    ("REVIEW", "REVIEW"): LifecycleEdge(
        "REVIEW", "REVIEW", "risk_level == high ⇒ 落 /approvals 待人工（HITL）", True
    ),
    ("REVIEW", "CANDIDATE"): LifecycleEdge(
        "REVIEW", "CANDIDATE", "人工驳回", True
    ),
    ("PUBLISHED", "DEPRECATED"): LifecycleEdge(
        "PUBLISHED", "DEPRECATED", "retire（人工审批）", True
    ),
    ("PUBLISHED", "CANDIDATE"): LifecycleEdge(
        "PUBLISHED", "CANDIDATE", "新修订：新建候选，current_version 不动", False
    ),
    ("DEPRECATED", "PUBLISHED"): LifecycleEdge(
        "DEPRECATED", "PUBLISHED", "复用 rollback（人工审批）", True
    ),
}


def can_transition(from_state: str, to_state: str) -> bool:
    """Return whether ``from_state → to_state`` is a legal lifecycle move."""
    return (from_state, to_state) in LIFECYCLE_TRANSITIONS


def assert_transition(from_state: str, to_state: str) -> str:
    """Return ``to_state`` if the move is legal, else raise ``403`` (fail-closed)."""
    if not can_transition(from_state, to_state):
        raise GovernanceError(
            f"illegal skill lifecycle transition {from_state} → {to_state}",
            status_code=403,
        )
    return to_state


def transition_requires_approval(from_state: str, to_state: str) -> bool:
    """Whether the move needs ``approve:skills``. Illegal moves raise ``403``."""
    edge = LIFECYCLE_TRANSITIONS.get((from_state, to_state))
    if edge is None:
        raise GovernanceError(
            f"illegal skill lifecycle transition {from_state} → {to_state}",
            status_code=403,
        )
    return edge.requires_approve


def derive_lifecycle(
    candidate: SkillCandidateRecord | None = None,
    skill: Any | None = None,
    *,
    contract: SkillContract | None = None,
) -> str:
    """Derive the 6-state lifecycle from existing records (read-only view).

    Precedence: a live ``skill`` record wins, then the ``candidate`` status. A
    ``draft`` candidate is ``CANDIDATE`` only when its (passed) ``contract`` is
    complete, else ``DRAFT`` — the one place the two record states share a value.
    """
    if skill is not None:
        status = getattr(skill, "status", "")
        if status == "published":
            return "PUBLISHED"
        if status == "retired":
            return "DEPRECATED"
        if status == "evaluating":
            return "TESTING"
    if candidate is not None:
        status = getattr(candidate, "status", "")
        if status == "promoted":
            return "PUBLISHED"
        if status == "approved":
            return "REVIEW"
        if status == "evaluating":
            return "TESTING"
        if status == "draft":
            return "CANDIDATE" if (contract is not None and contract.is_complete()) else "DRAFT"
        return "DRAFT"  # rejected / insufficient ⇒ honest fall-back to DRAFT
    return "DRAFT"


@dataclass
class SkillEngineeringResult:
    """The full record of one engineering-loop run (archive-friendly)."""

    tenant_id: str = ""
    candidate_id: str = ""
    lifecycle: str = "DRAFT"
    contract: SkillContract = field(default_factory=SkillContract)
    critique: SkillCritique = field(default_factory=SkillCritique)
    test_cases: list[SkillTestCase] = field(default_factory=list)
    test_runs: list[SkillTestRun] = field(default_factory=list)
    evaluation: SkillEvaluation = field(default_factory=SkillEvaluation)
    revisions: list[SkillRevision] = field(default_factory=list)
    rounds: int = 0
    passed: bool = False
    degraded_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "candidate_id": self.candidate_id,
            "lifecycle": self.lifecycle,
            "contract": self.contract.to_dict(),
            "critique": self.critique.to_dict(),
            "test_cases": [c.to_dict() for c in self.test_cases],
            "test_runs": [r.to_dict() for r in self.test_runs],
            "evaluation": self.evaluation.to_dict(),
            "revisions": [r.to_dict() for r in self.revisions],
            "rounds": self.rounds,
            "passed": self.passed,
            "degraded_reason": self.degraded_reason,
        }


def _reconcile_contract_tools(contract: SkillContract) -> None:
    """Force ``contract.tools`` ⊆ platform catalogue (order + dedupe preserved)."""
    catalogue = allowed_tool_set()
    kept: list[str] = []
    for tool in contract.tools:
        name = str(tool or "").strip()
        if name and name in catalogue and name not in kept:
            kept.append(name)
    contract.tools = kept


async def synthesize(
    tenant_id: str | None,
    experience_ids: list[str] | None = None,
    mode: str = "auto",
    *,
    candidate_repo: Any | None = None,
    experience_repo: Any | None = None,
) -> SkillContract:
    """**③** Compile a candidate (reuse ``candidate_compiler``) → ``SkillContract``.

    Raises ``GovernanceError(403)`` on an unresolved tenant (BE-5) and
    ``InsufficientExperiencesError`` when too few similar experiences exist.
    """
    _candidate, contract = await _compile_contract(
        tenant_id,
        experience_ids,
        mode,
        candidate_repo=candidate_repo,
        experience_repo=experience_repo,
    )
    return contract


async def _compile_contract(
    tenant_id: str | None,
    experience_ids: list[str] | None,
    mode: str,
    *,
    candidate_repo: Any | None,
    experience_repo: Any | None,
) -> tuple[SkillCandidateRecord, SkillContract]:
    """Shared compile path returning both the candidate and its contract."""
    tenant = require_tenant(tenant_id)  # BE-5 — fail closed before any read
    candidate = await compile_candidate(
        tenant,
        experience_ids,
        mode,
        candidate_repo=candidate_repo,
        experience_repo=experience_repo,
    )
    if candidate.status == "insufficient":
        raise InsufficientExperiencesError(
            found=len(candidate.experience_ids),
            required=get_settings().skill_candidate_min_experiences,
        )
    contract = SkillContract.from_draft_spec(candidate.draft_spec)
    _reconcile_contract_tools(contract)
    return candidate, contract


def sandbox_evaluate(
    contract: SkillContract, cases: list[SkillTestCase]
) -> SkillEvaluation:
    """**⑥** Run ``cases`` in the sandbox and aggregate an evaluation.

    ``pass_rate`` = ``passed / total`` (total includes ``error``);
    ``verified_pass_rate`` denominator **excludes** ``error`` (only decided
    ``pass``/``fail`` runs), so a sandbox failure can never inflate a rate.
    ``failure_modes`` are the failed cases' assertion tokens (stable, deduped).
    """
    runs = run_tests(contract, cases)
    total = len(runs)
    passed = sum(1 for r in runs if r.verdict == "pass")
    failed = sum(1 for r in runs if r.verdict == "fail")
    errored = sum(1 for r in runs if r.verdict == "error")
    verified_denom = passed + failed
    pass_rate = round(passed / total, 4) if total else 0.0
    verified_pass_rate = round(passed / verified_denom, 4) if verified_denom else 0.0
    failure_modes = sorted(
        {
            case.assertion
            for case, run in zip(cases, runs)
            if run.verdict == "fail"
        }
    )
    # ``errored`` is intentionally surfaced only through ``sample_size`` vs the
    # verified denominator — never folded into a "pass".
    if errored and not verified_denom:
        verified_pass_rate = 0.0
    return SkillEvaluation(
        pass_rate=pass_rate,
        verified_pass_rate=verified_pass_rate,
        failure_modes=failure_modes,
        sample_size=total,
        ran_at=utcnow().isoformat(),
    )


def _tests_passed(evaluation: SkillEvaluation) -> bool:
    """A run is 'passed' only with a non-empty sample at/above the threshold."""
    return evaluation.sample_size > 0 and evaluation.pass_rate >= TEST_PASS_THRESHOLD


async def _persist_loop_evaluation(
    cand_repo: Any,
    tenant: str,
    candidate_id: str,
    contract: SkillContract,
    evaluation: SkillEvaluation,
    verdict: str,
    *,
    candidate: SkillCandidateRecord | None = None,
) -> float:
    """Persist the loop's own evaluation (verdict ``pass``/``fail``); return score.

    This closes the INC44 §1.1 P1 gap: ``run_engineering_loop`` was a *pure*
    computation, so a passing loop left no ``SkillEvaluationRecord`` and the
    publish gate (``governance_gate.promote_candidate``) therefore always 403'd
    with "candidate has no evaluation". Here the loop records what it *actually*
    measured:

    * ``metrics["score"]`` reuses :func:`tester.structural_score` (the evaluator's
      exact weights / threshold), so this record shares **one** yardstick with
      ``evaluate_candidate`` and with the release gate's ``eval_score``;
    * the sandbox facts (``pass_rate`` / ``verified_pass_rate`` / ``sample_size``
      / ``failure_modes``) ride along for traceability — never a fabricated value;
    * on a ``pass`` the repaired contract is written back onto
      ``candidate.draft_spec`` and the candidate moves to ``approved`` (⇔ the
      ``REVIEW`` state, §3.3 mapping) — so the spec that gets *published* is
      exactly the spec that was *verified*;
    * on a ``fail`` the candidate's status is left untouched (an honest downgrade)
      and only the record is stored, so a later ``promote`` still 403s.

    This is purely an additive side effect: ``assert_transition`` is still called
    on every move, nothing is published here, and ``REVIEW → REVIEW`` (HITL) is
    unchanged.
    """
    from forgeflow.skills.models import SkillEvaluationRecord

    score = structural_score(contract)
    metrics: dict[str, Any] = {
        "score": score,
        "pass_rate": evaluation.pass_rate,
        "verified_pass_rate": evaluation.verified_pass_rate,
        "sample_size": evaluation.sample_size,
        "failure_modes": list(evaluation.failure_modes),
    }
    record = SkillEvaluationRecord(
        tenant_id=tenant,
        target_id=candidate_id,
        dataset="engineering_loop",
        metrics=metrics,
        verdict=verdict,
    )
    await cand_repo.save_evaluation(record)
    if verdict == "pass" and candidate is not None:
        candidate.draft_spec = contract.to_draft_spec()
        candidate.status = "approved"
        await cand_repo.save_candidate(candidate)
    return score


async def version_and_publish(
    tenant_id: str | None,
    candidate_id: str,
    actor: str,
    actor_role: str = "manager",
    *,
    candidate_repo: Any | None = None,
    skill_repo: Any | None = None,
    policy_repo: Any | None = None,
) -> SkillVersionRecord:
    """**⑦** Promote an evaluated candidate (reuse ``governance_gate``).

    Delegates to ``governance_gate.promote_candidate`` so RBAC (``approve:skills``
    else 403), the trust baseline, the release gate and the audit sink are the
    existing, tested implementations — not a second copy. A passing evaluation is
    required (enforced inside ``promote_candidate``).
    """
    tenant = require_tenant(tenant_id)
    return await promote_candidate(
        candidate_id,
        actor,
        tenant_id=tenant,
        candidate_repo=candidate_repo,
        skill_repo=skill_repo,
        policy_repo=policy_repo,
        actor_role=actor_role,
    )


async def run_engineering_loop(
    tenant_id: str | None,
    candidate_id: str,
    actor: str,
    actor_role: str = "manager",
    *,
    max_repair: int = 3,
    candidate_repo: Any | None = None,
    skill_repo: Any | None = None,
    policy_repo: Any | None = None,
) -> SkillEngineeringResult:
    """Run ④→⑤→⑥→⑦ over an existing candidate and return the full result.

    Walks the lifecycle ``DRAFT → CANDIDATE → TESTING → REVIEW`` with at most
    ``max_repair`` repair rounds (shared across the critique and test phases).
    If blockers survive or the rounds are exhausted, it **honestly degrades** to
    ``DRAFT`` — it never reports success it did not verify.
    """
    tenant = require_tenant(tenant_id)  # BE-5
    cand_repo = candidate_repo or get_skill_candidate_repository()
    sk_repo = skill_repo or get_skill_repository()
    pol_repo = policy_repo or get_policy_repository()

    candidate = await cand_repo.get_candidate(tenant, candidate_id)
    if candidate is None:
        raise GovernanceError("skill candidate not found", status_code=404)

    contract = SkillContract.from_draft_spec(candidate.draft_spec)
    _reconcile_contract_tools(contract)

    lifecycle = "DRAFT"
    revisions: list[SkillRevision] = []
    rounds = 0

    # --- critique gate: clear must_fix blockers (DRAFT → DRAFT in place) ----
    crit = critique(contract)
    while crit.must_fix and rounds < max_repair:
        rounds += 1
        assert_transition("DRAFT", "DRAFT")
        contract, revision = repair(contract, crit, None, rounds)
        revisions.append(revision)
        crit = critique(contract)

    if crit.must_fix:
        return SkillEngineeringResult(
            tenant_id=tenant,
            candidate_id=candidate_id,
            lifecycle="DRAFT",
            contract=contract,
            critique=crit,
            revisions=revisions,
            rounds=rounds,
            passed=False,
            degraded_reason=f"critique 阻断项未清除：{', '.join(crit.must_fix)}",
        )

    assert_transition("DRAFT", "CANDIDATE")
    lifecycle = "CANDIDATE"

    # --- TESTING: generate + sandbox-evaluate, repair while failing ---------
    assert_transition("CANDIDATE", "TESTING")
    lifecycle = "TESTING"
    cases = generate_tests(contract)
    evaluation = sandbox_evaluate(contract, cases)

    while not _tests_passed(evaluation) and rounds < max_repair:
        assert_transition("TESTING", "CANDIDATE")
        rounds += 1
        contract, revision = repair(contract, crit, evaluation, rounds)
        revisions.append(revision)
        crit = critique(contract)
        assert_transition("CANDIDATE", "TESTING")
        cases = generate_tests(contract)
        evaluation = sandbox_evaluate(contract, cases)

    if not _tests_passed(evaluation):
        assert_transition("TESTING", "DRAFT")  # honest downgrade
        # INC44 §1.1 — record the *measured* failure (status unchanged): a later
        # ``promote`` must still 403 because the stored verdict is ``fail``.
        await _persist_loop_evaluation(
            cand_repo, tenant, candidate_id, contract, evaluation, "fail"
        )
        return SkillEngineeringResult(
            tenant_id=tenant,
            candidate_id=candidate_id,
            lifecycle="DRAFT",
            contract=contract,
            critique=crit,
            test_cases=cases,
            test_runs=run_tests(contract, cases),
            evaluation=evaluation,
            revisions=revisions,
            rounds=rounds,
            passed=False,
            degraded_reason=(
                f"修复轮次耗尽仍未通过：{', '.join(evaluation.failure_modes) or 'pass_rate 未达阈值'}"
            ),
        )

    # --- REVIEW (high risk stays in REVIEW for HITL) -----------------------
    assert_transition("TESTING", "REVIEW")
    lifecycle = "REVIEW"
    if contract.risk_level == HIGH_RISK:
        assert_transition("REVIEW", "REVIEW")  # HITL — no auto-publish

    # INC44 §1.1 — the success exit *records* what it verified and advances the
    # candidate to the REVIEW-compatible status, so the existing publish gate
    # (``governance_gate.promote_candidate``) now finds a passing evaluation AND
    # publishes exactly the contract that passed. Nothing is published here.
    await _persist_loop_evaluation(
        cand_repo, tenant, candidate_id, contract, evaluation, "pass", candidate=candidate
    )

    return SkillEngineeringResult(
        tenant_id=tenant,
        candidate_id=candidate_id,
        lifecycle=lifecycle,
        contract=contract,
        critique=crit,
        test_cases=cases,
        test_runs=run_tests(contract, cases),
        evaluation=evaluation,
        revisions=revisions,
        rounds=rounds,
        passed=True,
        degraded_reason="",
    )
