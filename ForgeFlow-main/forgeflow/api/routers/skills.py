"""Skill + skill-candidate routes (docs §4.1).

Two routers live here because the API surface spans two prefixes:
  * ``router``            → mounted at ``/skills``
  * ``candidates_router`` → mounted at ``/skill-candidates``

Promotion additionally requires ``approve:skills`` — the route map grants
``write:skills`` for ``POST /skill-candidates`` (which also covers evaluate), so
the stricter check is enforced in the handler.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from forgeflow.api.dependencies import get_current_user
from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.hub_schemas import (
    CandidateCreateRequest,
    CandidateListResponse,
    CandidateResponse,
    CanaryResolveRequest,
    CanaryResolveResponse,
    EvaluateRequest,
    EvaluationResponse,
    PromoteRequest,
    RollbackRequest,
    SkillCreateRequest,
    SkillListResponse,
    SkillResponse,
    SkillVersionCreateRequest,
    SkillVersionResponse,
)
from forgeflow.rbac.enforcer import RBACEnforcer
from forgeflow.rbac.models import UserContext
from forgeflow.repositories import (
    get_skill_candidate_repository,
    get_skill_repository,
)
from forgeflow.skills.candidate_compiler import compile_candidate
from forgeflow.skills.errors import GovernanceError, InsufficientExperiencesError
from forgeflow.skills.evaluator import evaluate_candidate
from forgeflow.skills.governance_gate import promote_candidate, resolve_canary
from forgeflow.skills.registry import SkillRegistry
from forgeflow.skills.versioning import create_version, diff_specs, rollback

logger = logging.getLogger(__name__)
router = APIRouter()
candidates_router = APIRouter()


def _skill_response(skill) -> SkillResponse:
    return SkillResponse(**skill.to_dict())


def _version_response(version) -> SkillVersionResponse:
    return SkillVersionResponse(**version.to_dict())


def _candidate_response(candidate) -> CandidateResponse:
    return CandidateResponse(**candidate.to_dict())


# --------------------------------------------------------------------------- #
# /skills                                                                      #
# --------------------------------------------------------------------------- #

@router.get("", response_model=SkillListResponse)
async def list_skills(
    domain: str | None = Query(None),
    q: str | None = Query(None),
    featured: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    tenant: str = Depends(resolve_tenant),
):
    registry = SkillRegistry()
    items, total = await registry.list_skills(
        tenant, domain=domain, q=q, featured=featured, limit=limit, offset=offset
    )
    return SkillListResponse(total=total, items=[_skill_response(s) for s in items])


@router.post("", response_model=SkillResponse)
async def create_skill(
    request: SkillCreateRequest,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    registry = SkillRegistry()
    existing = await registry.get_by_name(tenant, request.name)
    if existing is not None:
        raise HTTPException(status_code=409, detail="skill name already exists")
    skill = await registry.create(
        tenant,
        request.name,
        request.domain,
        owner=request.owner or user.user_id,
        description=request.description,
    )
    return _skill_response(skill)


@router.get("/evolution-advice")
async def skill_evolution_advice(tenant: str = Depends(resolve_tenant)) -> dict:
    """INC2-19 — usage-driven evolution *advice* (never an automatic mutation).

    Delegates to :func:`forgeflow.skills.evolution.advise`, which runs a
    rule pass first and then at most **one** batched LLM call (``LLM_PROVIDER=mock``
    ⇒ rule-only, zero calls) with a result cache.

    ``approval_required`` is always ``true``: per design verdict §7.5 an advice
    is never applied directly — it becomes a pending approval for a human to
    decide. This GET is side-effect free; submission to the approval queue is a
    separate action (see ``submit_advice_for_approval``).
    """
    from forgeflow.skills.evolution import advise

    advices = await advise(tenant)
    return {
        "tenant_id": tenant,
        "total": len(advices),
        "approval_required": True,
        "items": [advice.to_dict() for advice in advices],
    }


@router.post("/evolution-advice/{skill_id}/approval", status_code=201)
async def submit_evolution_advice_for_approval(
    skill_id: str,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
) -> dict:
    """§7.5 — turn a skill's evolution *advice* into a pending approval.

    The GET sibling above only *produces* advice (side-effect free). This POST is
    the explicit action that enqueues that advice into the human approval queue:
    it re-runs :func:`forgeflow.skills.evolution.advise` for the tenant, selects
    the advice for ``skill_id``, and persists one ``agent_approvals`` row per
    advice via :func:`submit_advice_for_approval`.

    The **skill itself is never mutated** — an approval is only a request; the
    optimise/retire change stays a separate, human-gated step. Because this route
    lives under the ``/skills`` prefix it inherits the ``("POST", "/skills")``
    ``write:skills`` grant (RBAC longest-prefix match ⇒ UNMAPPED stays 0).
    """
    from forgeflow.skills.evolution import advise, submit_advice_for_approval

    matched = [advice for advice in await advise(tenant) if advice.skill_id == skill_id]
    if not matched:
        raise HTTPException(status_code=404, detail="no evolution advice for this skill")

    approvals = await submit_advice_for_approval(tenant, matched, requester=user.user_id)
    logger.info(
        "evolution advice → approval | tenant=%s skill=%s created=%d",
        tenant,
        skill_id,
        len(approvals),
    )
    return {
        "tenant_id": tenant,
        "skill_id": skill_id,
        "created": len(approvals),
        "approvals": [approval.to_dict() for approval in approvals],
    }


@router.get("/{skill_id}/versions", response_model=list[SkillVersionResponse])
async def list_skill_versions(skill_id: str, tenant: str = Depends(resolve_tenant)):
    registry = SkillRegistry()
    versions = await registry.versions(tenant, skill_id)
    return [_version_response(v) for v in versions]


@router.post("/{skill_id}/versions", response_model=SkillVersionResponse)
async def create_skill_version(
    skill_id: str,
    request: SkillVersionCreateRequest,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    repo = get_skill_repository()
    skill = await repo.get_skill(tenant, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    spec = request.spec
    if request.semver:
        # Explicit semver: create the version row directly.
        from forgeflow.skills.models import SkillVersionRecord

        version = SkillVersionRecord(
            tenant_id=tenant,
            skill_id=skill.id,
            semver=request.semver,
            spec=spec,
            changelog=request.changelog or f"[{request.semver}] by {user.user_id}",
            approved_by=user.user_id,
        )
        await repo.add_version(tenant, version)
        skill.current_version = request.semver
        await repo.update_skill(skill)
    else:
        version = await create_version(
            repo,
            tenant,
            skill,
            spec,
            bump=request.bump,
            actor=user.user_id,
            changelog=request.changelog,
            approved_by=user.user_id,
        )
    return _version_response(version)


@router.post("/{skill_id}/rollback", response_model=SkillResponse)
async def rollback_skill(
    skill_id: str,
    request: RollbackRequest,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    repo = get_skill_repository()
    try:
        skill, diff = await rollback(
            repo, tenant, skill_id, request.to_version, actor=user.user_id
        )
    except GovernanceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    logger.info("skill rollback | skill=%s to=%s diff_empty=%s", skill_id, request.to_version, diff.get("is_empty"))
    return _skill_response(skill)


@router.post("/{skill_id}/canary/resolve", response_model=CanaryResolveResponse)
async def resolve_skill_canary(
    skill_id: str,
    request: CanaryResolveRequest,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """Resolve a skill's canary window (INC9 B1) — promote / hold / rollback.

    Runs the same-yardstick A/B decision (``skills/canary.decide_ab`` over the
    two versions' *recorded* metrics) and applies it. This route lives under the
    ``/skills`` prefix, so it inherits the ``("POST", "/skills")`` ``write:skills``
    grant (RBAC longest-prefix match ⇒ UNMAPPED stays 0). Like promotion, it
    additionally requires ``approve:skills`` — enforced here, **not** loosened
    (the route map cannot express it).
    """
    if not RBACEnforcer().check(user.role, "approve", "skills"):
        raise HTTPException(
            status_code=403,
            detail=f"role '{user.role}' cannot approve skills",
        )
    try:
        result = await resolve_canary(
            skill_id,
            tenant_id=tenant,
            actor=user.user_id,
            actor_role=user.role,
            canary_metrics=request.canary_metrics,
            incumbent_metrics=request.incumbent_metrics,
            sample_n=request.sample_n,
        )
    except GovernanceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    logger.info(
        "skill canary resolve | skill=%s action=%s severity=%s",
        skill_id,
        result.get("action"),
        result.get("severity"),
    )
    return CanaryResolveResponse(**result)


# --------------------------------------------------------------------------- #
# /skill-candidates                                                            #
# --------------------------------------------------------------------------- #

@candidates_router.get("", response_model=CandidateListResponse)
async def list_candidates(
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    tenant: str = Depends(resolve_tenant),
):
    repo = get_skill_candidate_repository()
    rows = await repo.list_candidates(tenant, status=status, limit=limit, offset=offset)
    return CandidateListResponse(total=len(rows), items=[_candidate_response(c) for c in rows])


@candidates_router.post("", response_model=CandidateResponse)
async def create_candidate(
    request: CandidateCreateRequest,
    tenant: str = Depends(resolve_tenant),
):
    """Compile a skill candidate from similar experiences (jump ③).

    Returns ``status="insufficient"`` (HTTP 200) when fewer than N similar
    experiences exist — callers render the "not enough experience yet" state.
    """
    try:
        candidate = await compile_candidate(
            tenant, request.experience_ids, request.mode
        )
    except InsufficientExperiencesError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _candidate_response(candidate)


@candidates_router.post("/{candidate_id}/evaluate", response_model=EvaluationResponse)
async def evaluate(
    candidate_id: str,
    request: EvaluateRequest,
    tenant: str = Depends(resolve_tenant),
):
    try:
        evaluation = await evaluate_candidate(
            candidate_id, request.dataset, tenant_id=tenant
        )
    except GovernanceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return EvaluationResponse(
        id=evaluation.id,
        target_id=evaluation.target_id,
        dataset=evaluation.dataset,
        metrics=evaluation.metrics,
        verdict=evaluation.verdict,
        created_at=evaluation.created_at,
    )


@candidates_router.post("/{candidate_id}/promote", response_model=SkillVersionResponse)
async def promote(
    candidate_id: str,
    request: PromoteRequest,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    # Governance gate needs approve:skills — enforced here (the route map grants
    # write:skills for the /skill-candidates prefix).
    if not RBACEnforcer().check(user.role, "approve", "skills"):
        raise HTTPException(
            status_code=403,
            detail=f"role '{user.role}' cannot approve skills",
        )
    actor = request.actor or user.user_id
    try:
        version = await promote_candidate(
            candidate_id, actor, tenant_id=tenant, actor_role=user.role
        )
    except GovernanceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return _version_response(version)


def spec_diff(old_spec: dict, new_spec: dict) -> dict:
    """Expose the spec diff helper to the router layer (used by the UI)."""
    return diff_specs(old_spec, new_spec)
