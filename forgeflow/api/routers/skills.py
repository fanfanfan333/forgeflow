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
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from forgeflow.api.dependencies import get_current_user
from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.hub_schemas import (
    ApprovePublishRequest,
    CandidateCreateRequest,
    CandidateListResponse,
    CandidateResponse,
    CanaryResolveRequest,
    CanaryResolveResponse,
    EvaluateRequest,
    EvaluationResponse,
    MergeApproveRequest,
    PromoteRequest,
    RollbackRequest,
    RolloutRollbackRequest,
    RolloutStageRequest,
    SkillCreateRequest,
    SkillListResponse,
    SkillResponse,
    SkillVersionCreateRequest,
    SkillVersionResponse,
)
from forgeflow.config import get_settings
from forgeflow.rbac.enforcer import RBACEnforcer
from forgeflow.rbac.models import UserContext
from forgeflow.repositories import (
    get_skill_candidate_repository,
    get_skill_repository,
)
from forgeflow.repositories.base import utcnow
from forgeflow.skills.candidate_compiler import compile_candidate
from forgeflow.skills.errors import GovernanceError, InsufficientExperiencesError
from forgeflow.skills.evaluator import evaluate_candidate
from forgeflow.skills.export_bundle import (
    SkillNotFoundError,
    bundle_to_dict,
    bundle_zip_bytes,
    export_skill_bundle,
)
from forgeflow.skills.governance_gate import promote_candidate, resolve_canary
from forgeflow.skills.registry import SkillRegistry
from forgeflow.skills.spec_validation import validate_io_schema
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
    # INC34 —— io_schema 是**作者声明**的输入/输出结构。它合法与否在此拒绝，
    # 而不是把一个结构上不可能的描述落库（未来确定性执行器会读到它）。**只在
    # spec 声明了 `io_schema` 时才校验**：未声明的自由 spec 逐字不变（向后兼容，
    # 既有自由 dict 不会被误伤）。原因中文、可读，原样透出（不收敛成「参数错误」）。
    if "io_schema" in spec:
        problems = validate_io_schema(spec.get("io_schema"))
        if problems:
            raise HTTPException(
                status_code=400,
                detail="技能 I/O 结构（io_schema）不合法：" + "；".join(problems),
            )
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


@router.get("/{skill_id}/export")
async def export_skill(
    skill_id: str,
    format: str = Query("json"),  # noqa: A002 — 查询参数名，按 DoD 固定
    tenant: str = Depends(resolve_tenant),
) -> Any:
    """Export one skill + its current version as a portable JSON document.

    For team reuse / backup / migration (INC34). Read-only and tenant-scoped.
    The document is self-describing (``format`` / ``format_version``) so a future
    importer can reject an unknown schema instead of silently mis-reading it.
    A skill with no current version exports ``version: null`` — honestly, rather
    than a fabricated placeholder.

    INC46 T11 — **这是「扩展既有端点」而非新增端点。** 通过新增可选查询参数
    ``format`` 扩展：

    * ``format="json"``（**默认**）—— 与扩展前**逐字节不变**的既有 JSON 响应，
      既有调用的语义、字段、状态码全部原样保留；
    * ``format="bundle"`` —— 返回物化 bundle 的 JSON（:func:`bundle_to_dict`），
      含 ``files`` / ``content_hash`` / ``validation`` / ``skills_ref`` 等；
    * ``format="zip"`` —— 返回物化 bundle 的 zip 字节
      （``Response(media_type="application/zip")``）。

    物化以 **DB 为权威源**（``export_skill_bundle`` 每次重新读仓储），无该 skill
    ⇒ **诚实 404**（``SkillNotFoundError`` → ``HTTPException(404)``），绝不返回
    空壳 SKILL.md。未知 ``format`` 值回落到默认 ``json`` 分支（向后兼容）。

    Lives under the ``/skills`` prefix, so it inherits the ``("GET", "/skills")``
    ``read:skills`` grant via RBAC longest-prefix match (UNMAPPED stays 0).
    """
    if format == "zip":
        try:
            bundle = await export_skill_bundle(skill_id, tenant)
        except SkillNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Response(content=bundle_zip_bytes(bundle), media_type="application/zip")

    if format == "bundle":
        try:
            bundle = await export_skill_bundle(skill_id, tenant)
        except SkillNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return bundle_to_dict(bundle)

    # 默认分支：扩展前行为逐字节不变（既有 JSON 文档）。
    repo = get_skill_repository()
    skill = await repo.get_skill(tenant, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    version_payload: dict | None = None
    if skill.current_version:
        version = await repo.get_version(tenant, skill_id, skill.current_version)
        if version is not None:
            version_payload = _version_response(version).model_dump(mode="json")
    return {
        "format": "forgeflow.skill",
        "format_version": 1,
        "exported_at": utcnow().isoformat(),
        "skill": _skill_response(skill).model_dump(mode="json"),
        "version": version_payload,
    }


@router.post("/{skill_id}/versions/{semver}/approve-publish")
async def approve_publish_version(
    skill_id: str,
    semver: str,
    request: ApprovePublishRequest,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
) -> dict:
    """INC46 T15 — Level-1: approve a ``pending_approval`` version for publish.

    The publish interlock (``skills/publish_interlock.py``) stages every
    regression-passing evolution candidate as ``pending_approval`` while the
    R1–R8 guardrails are unmet; this route is the explicit human action that
    publishes one. It lives under the ``/skills`` prefix, so it inherits the
    ``("POST", "/skills")`` ``write:skills`` grant (RBAC longest-prefix match
    ⇒ UNMAPPED stays 0). Like promotion / canary-resolve it additionally
    requires ``approve:skills`` — enforced here, **not** loosened.

    Every refusal is explicit: 403 (no publish permission / Level-1 未开放 /
    回归复核未通过), 404 (跨租户读不到), 409 (版本不在 pending_approval；
    rejected 为终态). Each successful publish writes a ``publish_approvals``
    record (migration 022).
    """
    if not RBACEnforcer().check(user.role, "approve", "skills"):
        raise HTTPException(
            status_code=403,
            detail=f"role '{user.role}' cannot approve skills",
        )
    from forgeflow.skills.publish_interlock import approve_publish

    try:
        result = await approve_publish(
            tenant,
            skill_id,
            semver,
            approver=user.user_id,
            approver_role=user.role,
            reason=request.reason,
        )
    except GovernanceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    logger.info(
        "skill publish approved | tenant=%s skill=%s version=%s by=%s",
        tenant,
        skill_id,
        semver,
        user.user_id,
    )
    return result


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
# INC46 T34 — 灰度发布与自动回滚 (Canary rollout & auto-rollback)               #
# --------------------------------------------------------------------------- #

@router.get("/{skill_id}/rollouts")
async def list_skill_rollouts(
    skill_id: str,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
) -> dict:
    """列出该 skill 的灰度记录 + 各阶段指标 + 回滚账（只读）。

    继承 ``("GET", "/skills")`` 的 ``read:skills``（RBAC 最长前缀匹配）。
    租户 fail-closed：store 以 ``tenant_id`` 为第一谓词，跨租户读不到。
    """
    from forgeflow.rollout.controller import rollout_enabled
    from forgeflow.rollout.store import get_rollout_store

    store = get_rollout_store()
    rollouts = store.list_rollouts(tenant, skill_id)
    return {
        "skill_id": skill_id,
        "tenant_id": tenant,
        "enabled": rollout_enabled(tenant),
        "rollouts": [
            {
                **r.to_dict(),
                "metrics": [m.to_dict() for m in store.list_metrics(tenant, r.id)],
                "rollbacks": [b.to_dict() for b in store.list_rollbacks(tenant, r.id)],
            }
            for r in rollouts
        ],
    }


@router.post("/{skill_id}/rollouts/{rollout_id}/promote")
async def promote_skill_rollout(
    skill_id: str,
    rollout_id: str,
    request: RolloutStageRequest,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
) -> dict:
    """评审当前灰度档：样本足够则推进 / 封顶提升，触发回滚条件则**自动回滚**。

    客户端只提交**观测**（T16 标签 / 延迟 / 成本），指标由服务端推导。
    继承 ``("POST", "/skills")`` 的 ``write:skills``；像 promotion / canary-resolve
    一样**额外**要求 ``approve:skills`` —— 在 handler 强制，不放松（路径前缀表达不了）。
    """
    if not RBACEnforcer().check(user.role, "approve", "skills"):
        raise HTTPException(
            status_code=403,
            detail=f"role '{user.role}' cannot approve skills",
        )
    from forgeflow.rollout.controller import (
        apply_stage_decision,
        decide_stage,
        record_stage_metrics,
    )
    from forgeflow.rollout.metrics import derive_metrics
    from forgeflow.rollout.store import RolloutError, get_rollout_store

    store = get_rollout_store()
    rollout = store.get_rollout(tenant, rollout_id)
    if rollout is None or rollout.skill_id != skill_id:
        raise HTTPException(status_code=404, detail="rollout not found")

    candidate = derive_metrics(
        request.labels or [],
        latencies_ms=request.latencies_ms,
        costs=request.costs,
        window_hours=request.window_hours,
    )
    incumbent = derive_metrics(
        request.incumbent_labels or [],
        latencies_ms=request.incumbent_latencies_ms,
        costs=request.incumbent_costs,
        window_hours=request.incumbent_window_hours,
    )
    try:
        record_stage_metrics(tenant, rollout, candidate, store=store)
        decision = decide_stage(
            candidate,
            incumbent,
            current_pct=rollout.stage_pct,
            dangerous_event=request.dangerous_event,
        )
        outcome = apply_stage_decision(tenant, rollout, decision, store=store)
    except RolloutError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    logger.info(
        "skill rollout stage | skill=%s rollout=%s action=%s pct=%s",
        skill_id,
        rollout_id,
        decision.action,
        outcome.get("rollout", {}).get("stage_pct"),
    )
    return {
        "skill_id": skill_id,
        "metrics": candidate.to_dict(),
        "incumbent_metrics": incumbent.to_dict(),
        "decision": decision.to_dict(),
        "action": outcome["action"],
        "rollout": outcome["rollout"],
        "rollback_id": outcome["rollback_id"],
    }


@router.post("/{skill_id}/rollouts/{rollout_id}/rollback")
async def rollback_skill_rollout(
    skill_id: str,
    rollout_id: str,
    request: RolloutRollbackRequest,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
) -> dict:
    """**人工回滚**：新增回滚记录 + 流量指针切回 incumbent（只追加，不删改历史）。

    与 promotion / canary-resolve 一样要求 ``approve:skills`` —— 无权限 ⇒ 403
    （红线 5：不绕过 RBAC）。红线 6 / 20：回滚只**新增**记录，候选版本可审计保留。
    """
    if not RBACEnforcer().check(user.role, "approve", "skills"):
        raise HTTPException(
            status_code=403,
            detail=f"role '{user.role}' cannot approve skills",
        )
    from forgeflow.rollout.controller import manual_rollback
    from forgeflow.rollout.store import RolloutError, get_rollout_store

    store = get_rollout_store()
    rollout = store.get_rollout(tenant, rollout_id)
    if rollout is None or rollout.skill_id != skill_id:
        raise HTTPException(status_code=404, detail="rollout not found")
    try:
        record = manual_rollback(
            tenant,
            rollout,
            store=store,
            reason=request.reason or "",
            actor=user.user_id,
        )
    except RolloutError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    logger.info(
        "skill rollout rolled back (manual) | skill=%s rollout=%s by=%s",
        skill_id,
        rollout_id,
        user.user_id,
    )
    return {
        "skill_id": skill_id,
        "action": "rollback",
        "rollback": record.to_dict(),
        "rollout": rollout.to_dict(),
    }


# --------------------------------------------------------------------------- #
# INC46 T35 — Skill 生命周期治理 (lifecycle: proposals / state)                  #
# --------------------------------------------------------------------------- #

@router.get("/lifecycle/proposals")
async def list_lifecycle_proposals(
    status: str | None = Query(None, description="proposed | approved | rejected"),
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
) -> dict:
    """列出去重 / 合并提案（只读；**不自动合并**）。

    继承 ``("GET", "/skills")`` 的 ``read:skills``（RBAC 最长前缀匹配）。
    租户 fail-closed：store 以 ``tenant_id`` 为第一谓词，跨租户读不到。
    """
    from forgeflow.lifecycle.store import get_lifecycle_store

    store = get_lifecycle_store()
    return {
        "tenant_id": tenant,
        "proposals": [p.to_dict() for p in store.list_proposals(tenant, status=status)],
    }


@router.post("/lifecycle/proposals/{proposal_id}/approve")
async def approve_lifecycle_proposal(
    proposal_id: str,
    request: MergeApproveRequest,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
) -> dict:
    """**人工 approve** 一条合并提案：落 ``approved``，产出**新** skill（保留来源链）。

    与 promotion / rollouts 一样要求 ``approve:skills`` —— 无权限 ⇒ 403（红线 5）。
    合并**只新增** skill / 版本，两个源 skill 与其历史版本一字不动（红线 6）。
    """
    if not RBACEnforcer().check(user.role, "approve", "skills"):
        raise HTTPException(
            status_code=403,
            detail=f"role '{user.role}' cannot approve skills",
        )
    from forgeflow.lifecycle.similarity import approve_proposal, plan_merged_skill
    from forgeflow.lifecycle.state_machine import LifecycleError
    from forgeflow.lifecycle.store import get_lifecycle_store
    from forgeflow.skills.models import SkillVersionRecord

    store = get_lifecycle_store()
    proposal = store.get_proposal(tenant, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="proposal not found")

    registry = SkillRegistry()
    primary = await registry.get(tenant, proposal.primary_skill_id)
    duplicate = await registry.get(tenant, proposal.duplicate_skill_id)

    plan: dict | None = None
    merged_skill_id: str | None = None
    if primary is not None and duplicate is not None:
        plan = plan_merged_skill(proposal, primary, duplicate)
        merged = await registry.create(
            tenant,
            name=f"{plan['skill']['name']}（合并 {proposal.id[:6]}）",
            domain=plan["skill"]["domain"],
            description=plan["skill"]["description"],
            tags=plan["skill"]["tags"],
            status="draft",
        )
        repo = get_skill_repository()
        await repo.add_version(
            tenant,
            SkillVersionRecord(
                tenant_id=tenant,
                skill_id=merged.id,
                semver=plan["version"]["semver"],
                spec=plan["version"]["spec"],
                changelog=plan["version"]["changelog"],
                approved_by=user.user_id,
            ),
        )
        merged.current_version = plan["version"]["semver"]
        await repo.update_skill(merged)
        merged_skill_id = merged.id

    try:
        record = approve_proposal(
            store,
            tenant,
            proposal_id,
            actor=user.user_id,
            merged_skill_id=merged_skill_id,
            now=utcnow(),
        )
    except LifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    logger.info(
        "skill merge proposal approved | proposal=%s by=%s merged_skill=%s",
        proposal_id,
        user.user_id,
        merged_skill_id,
    )
    return {
        "proposal": record.to_dict(),
        "source_skill_ids": list(proposal.source_skill_ids),
        "merged_skill_id": merged_skill_id,
        "plan": plan,
    }


@router.get("/{skill_id}/lifecycle")
async def get_skill_lifecycle(
    skill_id: str,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
) -> dict:
    """该 skill 的当前生命周期状态 + 迁移审计流（只读；证据用）。

    继承 ``("GET", "/skills")`` 的 ``read:skills``。``retrieval_excluded`` 直接反映
    T09 检索闸（``deprecated`` / ``archived`` 不可被检索）。
    """
    from forgeflow.lifecycle.state_machine import current_state, retrieval_excluded
    from forgeflow.lifecycle.store import get_lifecycle_store

    store = get_lifecycle_store()
    return {
        "skill_id": skill_id,
        "tenant_id": tenant,
        "state": current_state(store, tenant, skill_id),
        "retrieval_excluded": retrieval_excluded(store, tenant, skill_id),
        "events": [e.to_dict() for e in store.list_events(tenant, skill_id)],
    }


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
    response = _candidate_response(candidate)
    if candidate.status == "insufficient":
        # INC34 — surface the *configured* threshold (SKILL_CANDIDATE_MIN_EXPERIENCES)
        # so the caller can say "需要至少 N 条" without inventing the number. Only
        # set on the insufficient state; every other candidate leaves it ``None``.
        response.required_experiences = get_settings().skill_candidate_min_experiences
    return response


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
