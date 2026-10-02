"""INC43 S3 / BE-3 — Skill Engineering routes (read-only facts + loop trigger).

Two routers live here because the surface spans two prefixes:

  * ``router``            → mounted at ``/skills``
  * ``candidates_router`` → mounted at ``/skill-candidates``

The routes are thin: the router only does **auth + serialisation**. Every step
of the engineering loop lives in :mod:`forgeflow.skills.engineering`, which in
turn reuses the already-tested ``critic`` / ``tester`` / ``evaluator`` /
``governance_gate`` machinery — no second implementation of any rule.

BE-5 (tenant isolation) is enforced **first** in every handler: ``require_tenant``
raises ``GovernanceError(403)`` on an unresolved tenant *before* any record is
read, so the loop can never be run "for all tenants" and an ``admin`` role cannot
widen it (the gate is role-agnostic).

RBAC: these routes carry no new ``ROUTE_PERMISSION_MAP`` entries on purpose.
``GET``/``POST`` under ``/skills`` and ``/skill-candidates`` already map to
``read:skills`` / ``write:skills`` and the middleware matches the **longest
prefix**, so ``/skills/{id}/engineering`` and
``/skill-candidates/{id}/engineering`` inherit those grants (see
``rbac/policies.py``). ``UNMAPPED`` therefore stays 0.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException

from forgeflow.api.dependencies import get_current_user
from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.hub_schemas import (
    SkillContractResponse,
    SkillCritiqueResponse,
    SkillEngineeringResponse,
    SkillEvaluationSummaryResponse,
    SkillLifecycleResponse,
    SkillTestCaseResponse,
    SkillTestRunResponse,
)
from forgeflow.config import get_settings
from forgeflow.rbac.models import UserContext
from forgeflow.repositories import (
    get_skill_candidate_repository,
    get_skill_repository,
)
from forgeflow.skills.contracts import SkillContract, SkillEvaluation
from forgeflow.skills.critic import critique
from forgeflow.skills.engineering import (
    LIFECYCLE_STATES,
    LIFECYCLE_TRANSITIONS,
    TEST_PASS_THRESHOLD,
    SkillEngineeringResult,
    derive_lifecycle,
    run_engineering_loop,
)
from forgeflow.skills.errors import GovernanceError
from forgeflow.skills.tester import generate_tests, run_tests
from forgeflow.skills.tenant_scope import require_tenant

logger = logging.getLogger(__name__)

router = APIRouter()
candidates_router = APIRouter()


# --------------------------------------------------------------------------- #
# lifecycle derivation helpers (read-only — never add a stored enum)           #
# --------------------------------------------------------------------------- #

def _next_states(from_state: str) -> list[str]:
    """The states reachable from ``from_state`` by a legal transition."""
    return [to for (frm, to) in LIFECYCLE_TRANSITIONS if frm == from_state]


def _requires_approval(from_state: str) -> bool:
    """Whether **any** move out of ``from_state`` needs ``approve:skills`` (HITL)."""
    return any(
        edge.requires_approve
        for (frm, _to), edge in LIFECYCLE_TRANSITIONS.items()
        if frm == from_state
    )


def _tests_passed(evaluation: SkillEvaluation) -> bool:
    """Same yardstick the loop uses: a non-empty sample at/above the threshold."""
    return evaluation.sample_size > 0 and evaluation.pass_rate >= TEST_PASS_THRESHOLD


# --------------------------------------------------------------------------- #
# response projection                                                          #
# --------------------------------------------------------------------------- #

def _contract_response(contract: SkillContract) -> SkillContractResponse:
    return SkillContractResponse(**contract.to_dict())


def _engineering_response(
    *,
    tenant_id: str,
    lifecycle: str,
    contract: SkillContract,
    critique_obj,
    cases,
    runs,
    evaluation: SkillEvaluation,
    candidate_id: str = "",
    skill_id: str = "",
    revisions: list | None = None,
    rounds: int = 0,
    passed: bool = False,
    degraded_reason: str = "",
) -> SkillEngineeringResponse:
    return SkillEngineeringResponse(
        tenant_id=tenant_id,
        candidate_id=candidate_id,
        skill_id=skill_id,
        lifecycle=lifecycle,
        contract=_contract_response(contract),
        critique=SkillCritiqueResponse(**critique_obj.to_dict()),
        test_cases=[SkillTestCaseResponse(**c.to_dict()) for c in cases],
        test_runs=[SkillTestRunResponse(**r.to_dict()) for r in runs],
        evaluation=SkillEvaluationSummaryResponse(**evaluation.to_dict()),
        revisions=list(revisions or []),
        rounds=rounds,
        passed=passed,
        degraded_reason=degraded_reason,
        next_states=_next_states(lifecycle),
        requires_approval=_requires_approval(lifecycle),
    )


def _result_response(result: SkillEngineeringResult) -> SkillEngineeringResponse:
    """Project a loop result onto the shared response shape."""
    return _engineering_response(
        tenant_id=result.tenant_id,
        lifecycle=result.lifecycle,
        contract=result.contract,
        critique_obj=result.critique,
        cases=result.test_cases,
        runs=result.test_runs,
        evaluation=result.evaluation,
        candidate_id=result.candidate_id,
        revisions=[r.to_dict() for r in result.revisions],
        rounds=result.rounds,
        passed=result.passed,
        degraded_reason=result.degraded_reason,
    )


async def _archive_run(result: SkillEngineeringResult, actor: str) -> None:
    """Best-effort archive of a loop run into ``skill_engineering_runs``.

    The offline (memory) profile has neither an archive table nor a pool, so it
    is **skipped**, never faked. Any failure is swallowed: archiving must never
    break the response the caller actually asked for.
    """
    if get_settings().storage_backend.lower() != "postgres":
        return
    try:
        from forgeflow.database import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO skill_engineering_runs
                  (tenant_id, candidate_id, lifecycle, passed, rounds,
                   degraded_reason, contract, critique, evaluation, archive,
                   created_by)
                VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8::jsonb,$9::jsonb,$10::jsonb,$11)
                """,
                result.tenant_id,
                result.candidate_id,
                result.lifecycle,
                result.passed,
                result.rounds,
                result.degraded_reason,
                json.dumps(result.contract.to_dict(), ensure_ascii=False),
                json.dumps(result.critique.to_dict(), ensure_ascii=False),
                json.dumps(result.evaluation.to_dict(), ensure_ascii=False),
                json.dumps(result.to_dict(), ensure_ascii=False),
                actor,
            )
    except Exception:  # noqa: BLE001 — archiving is strictly best-effort
        logger.warning("skill engineering archive skipped", exc_info=True)


# --------------------------------------------------------------------------- #
# /skills/{skill_id}/engineering — read-only facts for an existing skill       #
# --------------------------------------------------------------------------- #

@router.get("/{skill_id}/engineering", response_model=SkillEngineeringResponse)
async def skill_engineering_facts(
    skill_id: str,
    tenant: str = Depends(resolve_tenant),
) -> SkillEngineeringResponse:
    """Read-only engineering facts for a **live skill** (no side effects).

    Lifts the skill's current version ``spec`` into a :class:`SkillContract`,
    runs the deterministic critic + tester **in memory only** (nothing is
    persisted) and reports the derived lifecycle. A missing skill — or one the
    caller's tenant does not own — is an honest ``404`` (the repo already fails
    closed, so a foreign id yields ``None``).
    """
    tenant = require_tenant(tenant)  # BE-5 — before any read
    repo = get_skill_repository()
    skill = await repo.get_skill(tenant, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")

    spec: dict = {}
    if skill.current_version:
        version = await repo.get_version(tenant, skill_id, skill.current_version)
        if version is not None:
            spec = version.spec
    contract = SkillContract.from_draft_spec(spec)
    crit = critique(contract)
    cases = generate_tests(contract)
    evaluation = _evaluate(contract, cases)
    lifecycle = derive_lifecycle(None, skill, contract=contract)
    return _engineering_response(
        tenant_id=tenant,
        lifecycle=lifecycle,
        contract=contract,
        critique_obj=crit,
        cases=cases,
        runs=run_tests(contract, cases),
        evaluation=evaluation,
        skill_id=skill_id,
        passed=_tests_passed(evaluation),
    )


@router.get("/{skill_id}/lifecycle", response_model=SkillLifecycleResponse)
async def skill_lifecycle(skill_id: str, tenant: str = Depends(resolve_tenant)):
    """Read-only six-state lifecycle projection for one skill (§3.3)."""
    tenant = require_tenant(tenant)  # BE-5
    skill = await get_skill_repository().get_skill(tenant, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    lifecycle = derive_lifecycle(None, skill)
    return SkillLifecycleResponse(
        skill_id=skill_id,
        lifecycle=lifecycle,
        states=list(LIFECYCLE_STATES),
        next_states=_next_states(lifecycle),
        requires_approval=_requires_approval(lifecycle),
    )


# --------------------------------------------------------------------------- #
# /skill-candidates/{candidate_id}/engineering                                 #
# --------------------------------------------------------------------------- #

@candidates_router.get("/{candidate_id}/engineering", response_model=SkillEngineeringResponse)
async def candidate_engineering_facts(
    candidate_id: str,
    tenant: str = Depends(resolve_tenant),
) -> SkillEngineeringResponse:
    """Read-only engineering facts for a **candidate** (no side effects)."""
    tenant = require_tenant(tenant)  # BE-5
    candidate = await get_skill_candidate_repository().get_candidate(tenant, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Skill candidate not found")
    contract = SkillContract.from_draft_spec(candidate.draft_spec)
    crit = critique(contract)
    cases = generate_tests(contract)
    evaluation = _evaluate(contract, cases)
    lifecycle = derive_lifecycle(candidate, None, contract=contract)
    return _engineering_response(
        tenant_id=tenant,
        lifecycle=lifecycle,
        contract=contract,
        critique_obj=crit,
        cases=cases,
        runs=run_tests(contract, cases),
        evaluation=evaluation,
        candidate_id=candidate_id,
        passed=_tests_passed(evaluation),
    )


@candidates_router.post("/{candidate_id}/engineering", response_model=SkillEngineeringResponse)
async def run_candidate_engineering(
    candidate_id: str,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
) -> SkillEngineeringResponse:
    """Trigger the engineering loop (④→⑤→⑥→⑦) over an existing candidate.

    Runs the full ``DRAFT → CANDIDATE → TESTING → REVIEW`` walk with at most
    ``max_repair`` fix rounds and returns the complete result. A candidate the
    caller's tenant does not own is a ``404`` (the repo fails closed); an
    unresolved tenant is a ``403`` (``require_tenant``). Publishing a skill
    remains a **separate**, ``approve:skills``-gated action
    (``POST /skill-candidates/{id}/promote``) — this route never publishes.
    """
    tenant = require_tenant(tenant)  # BE-5 — before any read
    try:
        result = await run_engineering_loop(tenant, candidate_id, user.user_id, user.role)
    except GovernanceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    await _archive_run(result, actor=user.user_id)
    logger.info(
        "skill engineering loop | tenant=%s candidate=%s lifecycle=%s passed=%s rounds=%d",
        tenant,
        candidate_id,
        result.lifecycle,
        result.passed,
        result.rounds,
    )
    return _result_response(result)


def _evaluate(contract: SkillContract, cases) -> SkillEvaluation:
    """Thin local alias so both read-only routes share one aggregation call."""
    from forgeflow.skills.engineering import sandbox_evaluate

    return sandbox_evaluate(contract, cases)
