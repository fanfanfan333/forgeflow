"""Skill + skill-candidate routes (docs §4.1).

Two routers live here because the API surface spans two prefixes:
  * ``router``            → mounted at ``/skills``
  * ``candidates_router`` → mounted at ``/skill-candidates``

Promotion additionally requires ``approve:skills`` — the route map grants
``write:skills`` for ``POST /skill-candidates`` (which also covers evaluate), so
the stricter check is enforced in the handler.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from forgeflow.api.dependencies import get_current_user
from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.hub_schemas import (
    ApprovePublishRequest,
    CanaryResolveRequest,
    CanaryResolveResponse,
    CandidateCreateRequest,
    CandidateListResponse,
    CandidateResponse,
    CandidateRevisionRequest,
    EvaluateRequest,
    EvaluationResponse,
    FunctionalEvaluateRequest,
    InstructionDraftRequest,
    InstructionSkillRequest,
    InstructionTrialRequest,
    MergeApproveRequest,
    PromoteRequest,
    RollbackRequest,
    RolloutRollbackRequest,
    RolloutStageRequest,
    RunHandleResponse,
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


@router.get("/resolve", response_model=SkillResponse)
async def resolve_skill(ref: str = Query(..., min_length=1), tenant: str = Depends(resolve_tenant)):
    registry = SkillRegistry()
    skill = await registry.get(tenant, ref)
    if skill is None:
        skill = await registry.get_by_name(tenant, ref)
    if skill is None or skill.status == "archived":
        raise HTTPException(status_code=404, detail="Skill not found")
    return _skill_response(skill)


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


@router.get("/builtin-packages")
async def list_builtin_packages(tenant: str = Depends(resolve_tenant)):
    from forgeflow.skills.builtin_packages import builtin_packages

    return {
        "items": [
            {key: value for key, value in item.items() if key != "files"}
            for item in builtin_packages()
        ]
    }


@router.post("/builtin-packages/{slug}/import", response_model=SkillResponse, status_code=201)
async def import_builtin_package(
    slug: str, user: UserContext = Depends(get_current_user), tenant: str = Depends(resolve_tenant)
):
    from forgeflow.skills.builtin_packages import builtin_packages
    from forgeflow.skills.package_import import package_spec

    item = next((item for item in builtin_packages() if item["slug"] == slug), None)
    if item is None:
        raise HTTPException(status_code=404, detail="Builtin skill not found")
    return await _save_imported_package(package_spec(item["files"]), user, tenant)


@router.post("/import", response_model=SkillResponse, status_code=201)
async def import_skill_package(
    request: Request,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """Import a root SKILL.md or ZIP as a draft, never activate uploaded code."""
    import zipfile

    from forgeflow.skills.package_import import MAX_PACKAGE_BYTES, read_package

    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_PACKAGE_BYTES:
            raise HTTPException(status_code=413, detail="技能包不能超过 2 MB")
    try:
        spec = read_package(
            bytes(data),
            zip_format=request.headers.get("content-type", "").split(";")[0] == "application/zip",
        )
    except (ValueError, UnicodeError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await _save_imported_package(spec, user, tenant)


async def _save_imported_package(spec: dict, user: UserContext, tenant: str):
    from forgeflow.skills.models import SkillVersionRecord

    repo = get_skill_repository()
    registry = SkillRegistry(repo=repo)
    name = spec["manifest"]["name"]
    if await registry.get_by_name(tenant, name) is not None:
        raise HTTPException(status_code=409, detail="技能名称已存在，请先修改包中的 name")
    skill = await registry.create(
        tenant,
        name,
        "技能包",
        owner=user.user_id,
        description=spec["manifest"]["description"],
        status="draft",
    )
    try:
        await repo.add_version(
            tenant,
            SkillVersionRecord(
                tenant_id=tenant,
                skill_id=skill.id,
                semver="1.0.0",
                spec=spec,
                changelog="Imported untrusted skill package; functional evaluation pending",
            ),
        )
        skill.current_version = "1.0.0"
        await repo.update_skill(skill)
    except Exception:
        await repo.delete_skill(tenant, skill.id)
        raise
    return _skill_response(skill)


@router.post("/instructions", response_model=SkillResponse, status_code=201)
async def create_instruction_skill(
    request: InstructionSkillRequest,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """Save an authorized instruction skill and its first immutable version."""
    if not RBACEnforcer().check(user.role, "approve", "skills"):
        raise HTTPException(status_code=403, detail="Skill activation requires approve:skills")
    if (
        not request.name.strip()
        or not request.description.strip()
        or len(request.instructions.strip()) < 10
    ):
        raise HTTPException(status_code=400, detail="请填写技能名称、用途和至少10个字符的使用指令")
    registry = SkillRegistry()
    trial = None
    if request.trial_run_id:
        from forgeflow.runtime.dispatcher import load_run_header

        trial = await load_run_header(tenant, request.trial_run_id)
        draft = trial.declared_inputs.get("instruction_draft", {}) if trial else {}
        if (
            trial is None
            or trial.actor_user_id != user.user_id
            or trial.status != "completed"
            or trial.outcome != "success"
            or draft.get("instructions") != request.instructions.strip()
        ):
            raise HTTPException(status_code=409, detail="试运行未通过或指令已变化，请重新试运行")
    if await registry.get_by_name(tenant, request.name.strip()):
        raise HTTPException(status_code=409, detail="技能名称已存在，请使用其他名称")
    skill = await registry.create(
        tenant,
        request.name.strip(),
        request.domain,
        owner=user.user_id,
        description=request.description.strip(),
    )
    from forgeflow.skills.models import SkillVersionRecord

    repo = get_skill_repository()
    try:
        await repo.add_version(
            tenant,
            SkillVersionRecord(
                tenant_id=tenant,
                skill_id=skill.id,
                semver="1.0.0",
                approved_by=user.user_id,
                spec={
                    "kind": "instruction",
                    "prompt": request.instructions.strip(),
                    "instructions": request.instructions.strip(),
                    "example": request.example,
                    "steps": [],
                    "tools": [],
                    "trial_run_id": request.trial_run_id,
                },
                source_experience_ids=[trial.experience_id]
                if trial and getattr(trial, "experience_id", None)
                else [],
            ),
        )
        skill.current_version = "1.0.0"
        skill.status = "published"
        await repo.update_skill(skill)
    except Exception:
        await repo.delete_skill(tenant, skill.id)
        raise
    from forgeflow.context.viking_sync import queue_skill

    await queue_skill(tenant, user.user_id, skill, {"instructions": request.instructions})
    return _skill_response(skill)


@router.post("/instructions/draft", response_model=InstructionSkillRequest)
async def draft_instruction_skill(
    request: InstructionDraftRequest, tenant: str = Depends(resolve_tenant)
):
    import asyncio
    import json

    from langchain_core.messages import HumanMessage, SystemMessage

    from forgeflow.models.provider import get_model

    model = await asyncio.to_thread(get_model, strong=True)
    if getattr(model, "_llm_type", "") == "mock":
        raise HTTPException(status_code=503, detail="真实模型不可用，未生成技能草案")
    try:
        structured = getattr(model, "with_structured_output", None)
        draft_model = structured(InstructionSkillRequest) if callable(structured) else model
        result = await asyncio.wait_for(
            draft_model.ainvoke(
                [
                    SystemMessage(
                        content="把用户目标写成可重复使用的技能指令。只输出JSON对象：name(简洁中文名)、description(一句用途)、instructions(Markdown，包含输入、输出、适用场景、步骤、成功标准、保留数据、风险与需要用户批准的边界)、example、domain。不声称已执行或通过评估。不编造平台工具或能力。平台支持文档/文本/Excel读取编辑、CSV统计、代码仓库代理、实际产物保存。"
                    ),
                    HumanMessage(content=request.goal),
                ]
            ),
            timeout=90,
        )
        if isinstance(result, InstructionSkillRequest):
            return result
        text = str(result.content).strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return InstructionSkillRequest.model_validate(json.loads(text))
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            "Instruction draft generation failed: %s: %s", type(exc).__name__, str(exc)[:300]
        )
        raise HTTPException(status_code=502, detail="模型未返回有效草案，请重试") from exc


@router.post("/instructions/try", response_model=RunHandleResponse)
async def try_instruction_skill(
    request: InstructionTrialRequest,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    from forgeflow.governance.policy_engine import evaluate_task_entry
    from forgeflow.runtime.dispatcher import RunCapacityError, get_run_dispatcher
    from forgeflow.runtime.orchestrator import RequestContext, TaskCreate

    decision = await evaluate_task_entry(user.user_id, user.role, request.intent, tenant_id=tenant)
    if not decision.allowed or decision.requires_approval:
        raise HTTPException(status_code=403, detail=decision.reason or "该任务需要人工审批")
    from forgeflow.api.routers.tasks import _admission_guard

    _admission_guard("generic")
    try:
        task = TaskCreate(
            intent=request.intent,
            context={
                "resources": request.resources,
                "instruction_draft": {
                    "name": request.skill.name,
                    "instructions": request.skill.instructions.strip(),
                },
            },
        )
        ctx = RequestContext(tenant_id=tenant, user_id=user.user_id, role=user.role)
        from forgeflow.runtime.input_scope import InputReferenceError, prepare_input_scope

        try:
            await prepare_input_scope(task, ctx)
        except InputReferenceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        handle = await get_run_dispatcher().dispatch(task, ctx)
    except RunCapacityError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RunHandleResponse(**handle.to_dict())


@router.get("/{skill_id}/instructions")
async def instruction_skill(skill_id: str, tenant: str = Depends(resolve_tenant)):
    import json

    registry = SkillRegistry()
    skill = await registry.get(tenant, skill_id)
    if skill is None or skill.status == "archived":
        raise HTTPException(status_code=404, detail="Skill not found")
    versions = await registry.versions(tenant, skill_id)
    version = next((v for v in versions if v.semver == skill.current_version), None)
    spec = version.spec if version else {}
    instructions = str(spec.get("instructions") or spec.get("prompt") or "")
    source = f"---\nname: {json.dumps(skill.name, ensure_ascii=False)}\ndescription: {json.dumps(skill.description, ensure_ascii=False)}\n---\n\n{instructions}\n"
    return {
        "id": skill.id,
        "name": skill.name,
        "description": skill.description,
        "status": skill.status,
        "version": skill.current_version,
        "instructions": instructions,
        "example": spec.get("example", ""),
        "source": spec.get("package_files", {}).get("SKILL.md", source),
        "kind": spec.get("kind", "instruction"),
        "resources": sorted(k for k in spec.get("package_files", {}) if k != "SKILL.md"),
    }


@router.get("/{skill_id}/usage")
async def instruction_skill_usage(skill_id: str, tenant: str = Depends(resolve_tenant)):
    from datetime import datetime

    from forgeflow.workspace.store import get_workspace_store

    skill = await SkillRegistry().get(tenant, skill_id)
    if skill is None or skill.status == "archived":
        raise HTTPException(status_code=404, detail="Skill not found")
    rows = await get_workspace_store().list_recent_for_tenant(tenant, limit=1000)
    matched = [row for row in rows if skill_id in (row.declared_inputs.get("skill_ids") or [])]
    terminal = [
        row for row in matched if row.status in {"completed", "failed", "aborted", "rejected"}
    ]
    durations = []
    for row in terminal:
        if row.completed_at:
            try:
                durations.append(
                    max(
                        0.0,
                        (
                            datetime.fromisoformat(row.completed_at)
                            - datetime.fromisoformat(row.created_at)
                        ).total_seconds(),
                    )
                )
            except (ValueError, TypeError):
                pass
    return {
        "skill_id": skill_id,
        "source": "workspace_runs.declared_inputs.skill_ids",
        "window_limit": 1000,
        "attempts": len(matched),
        "terminal_runs": len(terminal),
        "success_rate": sum(row.outcome == "success" for row in terminal) / len(terminal)
        if terminal
        else None,
        "average_seconds": sum(durations) / len(durations) if durations else None,
        "average_cost": None,
        "runs": [
            {
                "run_id": row.run_id,
                "intent": row.intent,
                "status": row.status,
                "outcome": row.outcome,
                "created_at": row.created_at,
            }
            for row in matched[:10]
        ],
    }


@router.post("/{skill_id}/instructions", response_model=SkillResponse)
async def update_instruction_skill(
    skill_id: str,
    request: InstructionSkillRequest,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    if not RBACEnforcer().check(user.role, "approve", "skills"):
        raise HTTPException(status_code=403, detail="Skill activation requires approve:skills")
    registry = SkillRegistry()
    skill = await registry.get(tenant, skill_id)
    if skill is None or skill.status == "archived":
        raise HTTPException(status_code=404, detail="Skill not found")
    versions = await registry.versions(tenant, skill_id)
    current = next((v for v in versions if v.semver == skill.current_version), None)
    if current and current.spec.get("kind") == "skill_package":
        raise HTTPException(
            status_code=409, detail="技能包请导出后编辑并以新名称导入，避免丢失附属资源"
        )
    if (
        not request.name.strip()
        or not request.description.strip()
        or len(request.instructions.strip()) < 10
    ):
        raise HTTPException(status_code=400, detail="请填写技能名称、用途和至少10个字符的使用指令")
    existing = await registry.get_by_name(tenant, request.name.strip())
    if existing is not None and existing.id != skill_id:
        raise HTTPException(status_code=409, detail="技能名称已存在，请使用其他名称")
    skill.name = request.name.strip()
    skill.description = request.description.strip()
    await create_version(
        get_skill_repository(),
        tenant,
        skill,
        {
            "kind": "instruction",
            "prompt": request.instructions.strip(),
            "instructions": request.instructions.strip(),
            "example": request.example,
            "steps": [],
            "tools": [],
        },
        actor=user.user_id,
        approved_by=user.user_id,
        changelog="更新使用指令",
    )
    from forgeflow.context.viking_sync import queue_skill

    await queue_skill(tenant, user.user_id, skill, {"instructions": request.instructions})
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
        skill, diff = await rollback(repo, tenant, skill_id, request.to_version, actor=user.user_id)
    except GovernanceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    logger.info(
        "skill rollback | skill=%s to=%s diff_empty=%s",
        skill_id,
        request.to_version,
        diff.get("is_empty"),
    )
    return _skill_response(skill)


# --------------------------------------------------------------------------- #
# INC54 §十一/§十二 —— Skill 生命周期管理（退役 / 恢复 / 删除草稿）              #
# --------------------------------------------------------------------------- #
# 状态词表用 ``skills.status`` 的存量值（``draft`` / ``published`` / ``retired``），
# 直接写字段，**不**走 ``lifecycle/state_machine.py``（那套是 draft/candidate/
# published/deprecated/archived，与 INC54 不同，混用会触发 IllegalTransition）。
# ``retired`` 映射到 DEPRECATED（skills/engineering.py），而
# eligibility.EXECUTABLE_STATUS == "published"，因此 ``retired`` 技能在运行期
# **不可执行** —— 正是「退役」想要的语义，且历史一字不删。


@router.post("/{skill_id}/retire", response_model=SkillResponse)
async def retire_skill(
    skill_id: str,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """退役一个技能（``published → retired``）：保留记录与全部历史，仅停止执行。

    只把 ``skills.status`` 置为 ``"retired"``（INC54 词表）。退役是**可逆**的
    （见 :func:`restore_skill`）：任何版本 / 版本历史一字不动，仅改变运行期是否可执行。
    技能不存在（含跨租户读不到）⇒ **404**，绝不静默成功。

    写操作：继承 ``("POST", "/skills")`` 的 ``write:skills``（RBAC 最长前缀匹配），
    并在 handler 取 ``get_current_user``（与 ``rollback`` 端点同惯例）。
    """
    skill = await SkillRegistry().set_status(tenant, skill_id, "retired")
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    logger.info("skill retired | tenant=%s skill=%s by=%s", tenant, skill_id, user.user_id)
    return _skill_response(skill)


@router.post("/{skill_id}/restore", response_model=SkillResponse)
async def restore_skill(
    skill_id: str,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """恢复一个已退役的技能（``retired → published``）：重新可在运行期执行。

    与 :func:`retire_skill` 互为逆操作，只把 ``skills.status`` 置回 ``"published"``。
    技能不存在（含跨租户读不到）⇒ **404**。

    写操作：同 ``retire``，继承 ``write:skills`` 并取 ``get_current_user``。
    """
    skill = await SkillRegistry().set_status(tenant, skill_id, "published")
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    logger.info("skill restored | tenant=%s skill=%s by=%s", tenant, skill_id, user.user_id)
    return _skill_response(skill)


@router.post("/{skill_id}/archive")
async def archive_skill(
    skill_id: str,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """Remove a skill from discovery and execution while retaining audit references."""
    skill = await SkillRegistry().get(tenant, skill_id)
    if skill is None or skill.status == "archived":
        raise HTTPException(status_code=404, detail="Skill not found")
    skill.status = "archived"
    await get_skill_repository().update_skill(skill)
    logger.info("skill archived | tenant=%s skill=%s by=%s", tenant, skill_id, user.user_id)
    from forgeflow.context.viking_sync import enqueue

    for identity in get_settings().openviking_user_keys:
        mapped_tenant, _, mapped_user = identity.partition(":")
        if mapped_tenant == tenant:
            await enqueue(tenant, mapped_user, "delete_skill", skill_id, {})
    return {"skill_id": skill_id, "deleted": True}


@router.delete("/{skill_id}", status_code=204)
async def delete_skill(
    skill_id: str,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """删除一个**草稿**技能（INC54 §十一）—— 只有从未发布的草稿可被硬删除。

    已发布 / 已退役的技能**不删除**（会丢失可审计历史），请改用**退役**
    （``POST /skills/{id}/retire``）下线。每个拒绝都是**显式**的：

    * 技能不存在（含跨租户读不到）⇒ **404**；
    * 当前状态不是 ``"draft"`` ⇒ **409**（消息说明「只有草稿可删除，已发布技能请改用退役」）；
    * 该技能已有版本记录（``registry.versions()`` 非空）⇒ **409**（引用完整性：
      有历史的技能不被硬删除，避免悬空版本与审计缺口）。

    租户 fail-closed：``delete_skill`` 以 ``tenant_id`` 为第一谓词，跨租户删不掉
    （读不到 ⇒ 404）。写操作继承 ``write:skills`` 并取 ``get_current_user``。
    """
    registry = SkillRegistry()
    skill = await registry.get(tenant, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    if skill.status != "draft":
        raise HTTPException(
            status_code=409,
            detail="只有草稿可删除，已发布技能请改用退役",
        )
    if await registry.versions(tenant, skill_id):
        raise HTTPException(
            status_code=409,
            detail="该技能已有版本记录，不可删除（引用完整性）；如需下线请改用退役",
        )
    deleted = await get_skill_repository().delete_skill(tenant, skill_id)
    if not deleted:
        # 竞态：校验与删除之间已被他人删除 / 跨租户命中不到 ⇒ 仍如实 404。
        raise HTTPException(status_code=404, detail="Skill not found")
    logger.info("skill draft deleted | tenant=%s skill=%s by=%s", tenant, skill_id, user.user_id)
    return Response(status_code=204)


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
        candidate = await compile_candidate(tenant, request.experience_ids, request.mode)
    except InsufficientExperiencesError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    response = _candidate_response(candidate)
    if candidate.status == "insufficient":
        # INC34 — surface the *configured* threshold (SKILL_CANDIDATE_MIN_EXPERIENCES)
        # so the caller can say "需要至少 N 条" without inventing the number. Only
        # set on the insufficient state; every other candidate leaves it ``None``.
        response.required_experiences = get_settings().skill_candidate_min_experiences
    return response


@candidates_router.delete("/{candidate_id}")
async def archive_candidate(candidate_id: str, tenant: str = Depends(resolve_tenant)):
    repo = get_skill_candidate_repository()
    record = await repo.get_candidate(tenant, candidate_id)
    if record is None or record.status == "archived":
        raise HTTPException(status_code=404, detail="Candidate not found")
    record.status = "archived"
    await repo.save_candidate(record)
    return {"candidate_id": candidate_id, "deleted": True}


# Local Ollama evaluations run serially and keep their real progress in the repository.
_FUNCTIONAL_JOBS: dict[tuple[str, str], asyncio.Task] = {}
_FUNCTIONAL_LIMIT = asyncio.Semaphore(1)
_FUNCTIONAL_ADMISSION = asyncio.Lock()


@candidates_router.get("/{candidate_id}/evaluation", response_model=EvaluationResponse)
async def latest_candidate_evaluation(
    candidate_id: str,
    tenant: str = Depends(resolve_tenant),
    kind: Literal["functional", "structural"] | None = None,
):
    repo = get_skill_candidate_repository()
    if await repo.get_candidate(tenant, candidate_id) is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    record = await repo.get_evaluation_for(tenant, candidate_id, kind=kind)
    if record is None:
        raise HTTPException(status_code=404, detail="No evaluation recorded")
    return EvaluationResponse(**record.to_dict())


@candidates_router.post(
    "/{candidate_id}/functional-evaluate", response_model=EvaluationResponse, status_code=202
)
async def start_functional_evaluation(
    candidate_id: str,
    request: FunctionalEvaluateRequest,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    from forgeflow.evaluation.golden_registry import (
        GoldenRegistryError,
        get_golden_registry,
        verify_content_hash,
    )
    from forgeflow.runtime.orchestrator import RequestContext
    from forgeflow.skills.evaluator import candidate_spec_hash
    from forgeflow.skills.functional_evaluation import evaluate_functionally
    from forgeflow.skills.models import SkillEvaluationRecord

    key = (tenant, candidate_id)
    if key in _FUNCTIONAL_JOBS:
        raise HTTPException(status_code=409, detail="该候选已有功能评测正在运行")
    if len(_FUNCTIONAL_JOBS) >= 2:
        raise HTTPException(status_code=503, detail="本地功能评测队列已满")
    repo = get_skill_candidate_repository()
    candidate = await repo.get_candidate(tenant, candidate_id)
    if candidate is None or candidate.status == "archived":
        raise HTTPException(status_code=404, detail="Candidate not found")
    dataset = await get_golden_registry().get_set(tenant, request.dataset)
    if dataset is None or not dataset.frozen:
        raise HTTPException(status_code=409, detail="请选择当前租户已冻结的独立评测集")
    try:
        dataset_hash = verify_content_hash(dataset)
    except GoldenRegistryError as exc:
        raise HTTPException(status_code=409, detail="评测集内容与冻结版本不一致") from exc
    incumbent = None
    if request.incumbent_skill_id:
        registry = SkillRegistry()
        skill = await registry.get(tenant, request.incumbent_skill_id)
        if skill is None or skill.status != "published":
            raise HTTPException(status_code=404, detail="Incumbent skill not found")
        incumbent = next(
            (
                version.spec
                for version in await registry.versions(tenant, skill.id)
                if version.semver == skill.current_version
            ),
            None,
        )
        if incumbent is None:
            raise HTTPException(status_code=409, detail="当前技能版本不存在")
    resume_record = None
    if request.resume_evaluation_id:
        resume_record = await repo.get_evaluation_for(tenant, candidate_id, kind="functional")
        if resume_record is None or resume_record.id != request.resume_evaluation_id:
            raise HTTPException(status_code=409, detail="评测记录已变化，请刷新后再恢复")
    initial = SkillEvaluationRecord(
        tenant_id=tenant,
        target_id=candidate_id,
        dataset=request.dataset,
        metrics={
            "evaluation_kind": "functional",
            "job_status": "queued",
            "functional_measured": False,
            "candidate_spec_hash": candidate_spec_hash(candidate.draft_spec),
            "dataset_hash": dataset_hash,
        },
    )
    if resume_record is not None:
        initial.metrics = {**resume_record.metrics, "job_status": "queued"}
    else:
        initial.metrics["job_id"] = initial.id
    ctx = RequestContext(tenant_id=tenant, user_id=user.user_id, role=user.role)

    async def run_evaluation():
        try:
            async with _FUNCTIONAL_LIMIT:
                await evaluate_functionally(
                    candidate_id,
                    request.dataset,
                    ctx,
                    incumbent_spec=incumbent,
                    job_id=initial.metrics["job_id"],
                    expected_spec_hash=initial.metrics["candidate_spec_hash"],
                    resume_from=resume_record,
                    include_ablation=request.include_ablation,
                )
        except BaseException as exc:
            failed = SkillEvaluationRecord(
                tenant_id=tenant,
                target_id=candidate_id,
                dataset=request.dataset,
                verdict="pending" if isinstance(exc, asyncio.CancelledError) else "fail",
                metrics={
                    **(
                        (
                            await repo.get_evaluation_for(tenant, candidate_id, kind="functional")
                            or initial
                        ).metrics
                    ),
                    "job_status": "interrupted"
                    if isinstance(exc, asyncio.CancelledError)
                    else "failed",
                    "reason": str(exc)[:300],
                },
            )
            await repo.save_evaluation(failed)
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            _FUNCTIONAL_JOBS.pop(key, None)

    async with _FUNCTIONAL_ADMISSION:
        if key in _FUNCTIONAL_JOBS:
            raise HTTPException(status_code=409, detail="该候选已有功能评测正在运行")
        if len(_FUNCTIONAL_JOBS) >= 2:
            raise HTTPException(status_code=503, detail="本地功能评测队列已满")
        await repo.save_evaluation(initial)
        _FUNCTIONAL_JOBS[key] = asyncio.create_task(run_evaluation())
    return EvaluationResponse(**initial.to_dict())


@candidates_router.post("/{candidate_id}/revise", response_model=CandidateResponse, status_code=201)
async def revise_failed_candidate(
    candidate_id: str, request: CandidateRevisionRequest, tenant: str = Depends(resolve_tenant)
):
    from forgeflow.skills.functional_evaluation import revise_candidate

    try:
        candidate = await revise_candidate(
            candidate_id, request.instructions, request.reason, tenant
        )
    except GovernanceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return CandidateResponse(**candidate.to_dict())


@candidates_router.post("/{candidate_id}/evaluate", response_model=EvaluationResponse)
async def evaluate(
    candidate_id: str,
    request: EvaluateRequest,
    tenant: str = Depends(resolve_tenant),
):
    try:
        evaluation = await evaluate_candidate(candidate_id, request.dataset, tenant_id=tenant)
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
