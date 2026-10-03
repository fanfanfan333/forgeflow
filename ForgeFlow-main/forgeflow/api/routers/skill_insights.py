"""INC46 T06 — Skill *insights* routes (rules / experience / readiness) + Forge.

Mounted at ``/skills`` (see ``api/main.py``). These are the "三栏增量"
(read-only) surfaces the Skill Center consumes, plus the one explicit write —
the Forge trigger that compiles a candidate from real experiences:

  * ``GET  /skills/{skill_id}/rules``      — the tenant's MUST / MUST_NOT rules
    with their **real** enforcement status (``tool_permissions`` single truth);
  * ``GET  /skills/{skill_id}/experience`` — the experiences that produced the
    skill's current version (``SkillVersion.source_experience_ids``);
  * ``GET  /skills/{skill_id}/readiness``  — the honest readiness facts. The
    measured ``rate`` is ``None`` (never ``0``) when nothing was evaluated
    (INC46 §8 红线 4：未测量 ⇒ None，绝不写 0);
  * ``POST /skills/forge``                 — compile a candidate from similar
    experiences (reuses ``candidate_compiler.compile_candidate`` — no second
    implementation);
  * ``GET  /skills/forge/{forge_id}``      — read back one Forge record.

Design boundaries (INC46 §5 T06 增补 v3)
----------------------------------------
* **No new DB table.** Reads consume only pre-existing storage: the memory /
  postgres rule store (migration ``020``), the SkillRepository
  (``skills`` / ``skill_versions``), and the ExperienceRepository
  (``experiences``, migration ``010``).
* **No T07+ import.** This module never imports ``skills/schemas.py`` /
  ``segments.py`` / ``contract_completion.py`` (T07) or anything landed after
  it. Its only skill-layer imports are T02 (``rule_assets``) and T04
  (``tool_permissions``).
* **Tenant fail-closed (红线 5).** Every handler resolves the tenant first
  (``require_tenant`` ⇒ 403 on an unresolved tenant) and every read is
  tenant-scoped by the repository. A cross-tenant id reads as ``404``; the
  Forge ledger additionally carries an **explicit** tenant predicate
  (:func:`_forge_record` — the single load-bearing isolation gate, and the
  documented counterfactual target).
* **Read-only where it says so.** ``rules`` / ``experience`` / ``readiness``
  have no side effects. Only ``POST /skills/forge`` writes (one candidate,
  scoped to the caller's tenant).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.hub_schemas import (
    ExperienceResponse,
    SkillEnforcementSummary,
    SkillExperienceResponse,
    SkillForgeRequest,
    SkillForgeResponse,
    SkillReadinessCheck,
    SkillReadinessResponse,
    SkillRuleItem,
    SkillRulesResponse,
)
from forgeflow.repositories import (
    get_experience_repository,
    get_skill_repository,
)
from forgeflow.skills.errors import GovernanceError
from forgeflow.skills.rule_assets import list_rules
from forgeflow.skills.tenant_scope import require_tenant

logger = logging.getLogger(__name__)
router = APIRouter()


# --------------------------------------------------------------------------- #
# tenant gate (fail-closed)                                                    #
# --------------------------------------------------------------------------- #
def _tenant_or_403(tenant: str | None) -> str:
    """Resolve the tenant, mapping ``GovernanceError`` to an explicit 403.

    ``require_tenant`` raises ``GovernanceError(403)`` on an empty/``None``
    tenant; the router must surface that as a real HTTP 403 rather than let an
    unhandled domain error become a 500.
    """
    try:
        return require_tenant(tenant)
    except GovernanceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


async def _owned_skill(repo: Any, tenant: str, skill_id: str) -> Any:
    """Fetch a skill **only** if the resolved tenant owns it; else ``404``.

    The repository already fails closed on a foreign id (returns ``None``), so
    a cross-tenant read is an honest 404 — never another tenant's row.
    """
    skill = await repo.get_skill(tenant, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return skill


async def _current_version(repo: Any, tenant: str, skill: Any) -> Any | None:
    """The skill's current ``SkillVersion`` record (``None`` when unpublished)."""
    semver = str(getattr(skill, "current_version", "") or "")
    if not semver:
        return None
    return await repo.get_version(tenant, str(getattr(skill, "id", "") or ""), semver)


def _to_experience_response(record: Any) -> ExperienceResponse:
    """Project an ``ExperienceRecord`` onto the frozen hub schema."""
    return ExperienceResponse(**record.to_dict())


# --------------------------------------------------------------------------- #
# rule → tool extraction (deterministic; the rule text is a fixed template)    #
# --------------------------------------------------------------------------- #
# ``rule_assets`` emits a *fixed template*, so the referenced tool id is
# recoverable without a generative step. A miss degrades to "工具未解析"
# (``enforced=False`` with an honest evidence string) — it never guesses.
_MUST_NOT_RE = re.compile(r"MUST NOT\s+直接调用\s+([^\s（(]+)")
_MUST_RE = re.compile(r"MUST\s+先执行\s+([^\s（(]+)\s+再执行\s+([^\s（(]+)")


def _rule_tools(rule_kind: str, rule_text: str) -> list[str]:
    """The tool id(s) a rule references, in template order (``[]`` on a miss)."""
    text = rule_text or ""
    if rule_kind == "must_not":
        match = _MUST_NOT_RE.search(text)
        return [match.group(1)] if match else []
    match = _MUST_RE.search(text)
    return [match.group(1), match.group(2)] if match else []


def _rule_enforced(
    rule_kind: str, rule_text: str, dangerous: frozenset[str]
) -> tuple[bool, str]:
    """Whether the platform **really** enforces a rule (vs. it being advisory).

    A ``must_not`` rule is enforced only when its tool is in the platform's
    ``DANGEROUS`` set — exactly what the publish/candidate gate blocks. Ordering
    (``must``) rules and any tool outside the dangerous set are advisory; the
    evidence string says which.
    """
    tools = _rule_tools(rule_kind, rule_text)
    if not tools:
        return False, "无法从规则文本解析工具 id；未判定为已强制"
    if rule_kind == "must_not":
        blocked = [tool for tool in tools if tool in dangerous]
        if blocked:
            return True, (
                f"平台发布门禁阻断 DANGEROUS 工具 {blocked[0]}"
                "（tool_permissions 单一真源）"
            )
        return False, "该工具不在平台 DANGEROUS 集；仅建议，未被强制阻断"
    return False, "先后顺序规则为建议；平台不强制排序"


def _enforcement_summary(spec: dict[str, Any]) -> SkillEnforcementSummary:
    """The skill's declared tools classified by the platform's single truth.

    ``blocked_tools`` is the intersection of the skill's declared tools with
    ``DANGEROUS_TOOLS()`` — the tools the platform genuinely blocks. Everything
    is derived from ``forgeflow.skills.tool_permissions``; no parallel table.
    """
    from forgeflow.skills.tool_permissions import DANGEROUS_TOOLS, class_of_skill

    classes = class_of_skill(spec or {})
    declared = sorted(key for key in classes if key not in ("overall", "risk_level"))
    dangerous = sorted(DANGEROUS_TOOLS())
    dangerous_set = set(dangerous)
    return SkillEnforcementSummary(
        source="forgeflow.skills.tool_permissions",
        declared_tools=declared,
        tool_classes={tool: str(classes[tool]) for tool in declared},
        dangerous_tools=dangerous,
        risk_level=str(classes.get("risk_level", "low")),
        blocked_tools=sorted(tool for tool in declared if tool in dangerous_set),
    )


# --------------------------------------------------------------------------- #
# /skills/forge  (registered BEFORE /{skill_id}/... so "forge" is never an id) #
# --------------------------------------------------------------------------- #
#: In-process Forge ledger (offline-friendly, tenant-keyed). No DB table — the
#: compiled candidate itself is persisted by ``compile_candidate``; this only
#: remembers the (tenant, forge_id) → result mapping so a second GET can read
#: it back. Mirrors the discipline of ``rule_assets._MEMORY``.
_FORGE: dict[str, dict[str, Any]] = {}


def reset_forge_registry() -> None:
    """Clear the in-process Forge ledger. Test helper only."""
    _FORGE.clear()


def _forge_record(forge_id: str, tenant: str) -> dict[str, Any]:
    """Read one Forge record, enforcing the tenant predicate.

    **This is the load-bearing tenant predicate** for the Forge surface: a
    record whose ``tenant_id`` is not the caller's is treated as absent
    (``404``). Deleting this comparison — a counterfactual — would let a
    cross-tenant ``GET /skills/forge/{id}`` leak another tenant's record; the
    integration suite pins that with
    ``tests/integration/test_inc46_insights_api.py::test_cross_tenant_forge_readback_is_404``
    (its owner-side read-back is the positive control).

    Citations here are ``file.py::symbol`` anchors, never ``file.py:line`` —
    line numbers drift as the suite grows.
    """
    record = _FORGE.get(forge_id)
    if record is None or str(record.get("tenant_id", "")) != tenant:
        raise HTTPException(status_code=404, detail="Forge record not found")
    return record


@router.post("/forge", response_model=SkillForgeResponse)
async def forge_skill(
    request: SkillForgeRequest,
    tenant: str = Depends(resolve_tenant),
) -> SkillForgeResponse:
    """Compile a skill candidate from real experiences ("锻造").

    Reuses :func:`forgeflow.skills.candidate_compiler.compile_candidate` — the
    one existing compiler — so there is no second draft path. Fewer than the
    configured minimum similar experiences yields an honest ``insufficient``
    record (HTTP 200): the caller renders "经验不足" with the real threshold,
    never a fabricated candidate. The record is written to the in-process Forge
    ledger, tenant-scoped.
    """
    tenant = _tenant_or_403(tenant)
    from forgeflow.skills.candidate_compiler import compile_candidate

    try:
        candidate = await compile_candidate(
            tenant, request.experience_ids, request.mode
        )
    except GovernanceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    insufficient = candidate.status == "insufficient"
    required: int | None = None
    if insufficient:
        from forgeflow.config import get_settings

        required = int(get_settings().skill_candidate_min_experiences)

    from forgeflow.repositories.base import new_id

    forge_id = f"forge-{new_id()}"
    record: dict[str, Any] = {
        "forge_id": forge_id,
        "tenant_id": tenant,
        "status": "insufficient" if insufficient else "compiled",
        "candidate_id": "" if insufficient else str(getattr(candidate, "id", "") or ""),
        "name": str(getattr(candidate, "name", "") or ""),
        "domain": str(getattr(candidate, "domain", "") or ""),
        "experience_ids": [str(x) for x in (getattr(candidate, "experience_ids", None) or [])],
        "similarity_score": float(getattr(candidate, "similarity_score", 0.0) or 0.0),
        "required_experiences": required,
    }
    _FORGE[forge_id] = record
    logger.info(
        "skill forge | tenant=%s forge=%s status=%s candidate=%s",
        tenant,
        forge_id,
        record["status"],
        record["candidate_id"],
    )
    return SkillForgeResponse(**record)


@router.get("/forge/{forge_id}", response_model=SkillForgeResponse)
async def forge_result(
    forge_id: str,
    tenant: str = Depends(resolve_tenant),
) -> SkillForgeResponse:
    """Read back one Forge record (tenant-scoped; cross-tenant ⇒ ``404``)."""
    tenant = _tenant_or_403(tenant)
    return SkillForgeResponse(**_forge_record(forge_id, tenant))


# --------------------------------------------------------------------------- #
# /skills/{skill_id}/rules                                                     #
# --------------------------------------------------------------------------- #
@router.get("/{skill_id}/rules", response_model=SkillRulesResponse)
async def skill_rules(
    skill_id: str,
    tenant: str = Depends(resolve_tenant),
) -> SkillRulesResponse:
    """A skill's tenant rules (``must`` / ``must_not``) + real enforcement.

    The rules are the tenant's deterministically-extracted rule assets
    (``skill_rule_suggestions``, migration ``020``). Each rule carries its
    recomputable ``support`` / ``confidence`` and — for ``must_not`` rules whose
    tool the platform really blocks — an ``enforced`` flag with its evidence.
    A skill the tenant does not own is an honest ``404``.
    """
    tenant = _tenant_or_403(tenant)
    repo = get_skill_repository()
    skill = await _owned_skill(repo, tenant, skill_id)
    version = await _current_version(repo, tenant, skill)
    spec = dict(getattr(version, "spec", None) or {}) if version is not None else {}

    from forgeflow.skills.tool_permissions import DANGEROUS_TOOLS

    dangerous = DANGEROUS_TOOLS()
    rows = await list_rules(tenant, limit=200)

    must: list[SkillRuleItem] = []
    must_not: list[SkillRuleItem] = []
    for rule in rows:
        enforced, evidence = _rule_enforced(rule.rule_kind, rule.rule_text, dangerous)
        item = SkillRuleItem(
            rule_id=str(rule.id),
            rule_kind=str(rule.rule_kind),
            rule_text=str(rule.rule_text),
            support=int(rule.support),
            confidence=float(rule.confidence),
            source_run_ids=[str(x) for x in (rule.source_run_ids or [])],
            enforced=enforced,
            enforcement=evidence,
        )
        (must_not if rule.rule_kind == "must_not" else must).append(item)

    return SkillRulesResponse(
        skill_id=skill_id,
        tenant_id=tenant,
        must=must,
        must_not=must_not,
        enforcement=_enforcement_summary(spec),
    )


# --------------------------------------------------------------------------- #
# /skills/{skill_id}/experience                                                #
# --------------------------------------------------------------------------- #
@router.get("/{skill_id}/experience", response_model=SkillExperienceResponse)
async def skill_experience(
    skill_id: str,
    tenant: str = Depends(resolve_tenant),
) -> SkillExperienceResponse:
    """The experiences that produced the skill's current version.

    Source = ``SkillVersion.source_experience_ids`` (the provenance link written
    when a version was created). Each id is fetched **tenant-scoped** from the
    ExperienceRepository; an id that no longer resolves (deleted / foreign) is
    skipped rather than fabricated. An unknown / cross-tenant skill ⇒ ``404``.
    """
    tenant = _tenant_or_403(tenant)
    repo = get_skill_repository()
    skill = await _owned_skill(repo, tenant, skill_id)
    version = await _current_version(repo, tenant, skill)
    source_ids = (
        [str(x) for x in (getattr(version, "source_experience_ids", None) or [])]
        if version is not None
        else []
    )

    exp_repo = get_experience_repository()
    items: list[ExperienceResponse] = []
    for experience_id in source_ids:
        record = await exp_repo.get(tenant, experience_id)
        if record is None:
            continue
        # Explicit tenant predicate: never surface a record we do not own.
        if str(getattr(record, "tenant_id", "") or "") != tenant:
            continue
        items.append(_to_experience_response(record))

    return SkillExperienceResponse(
        skill_id=skill_id, tenant_id=tenant, total=len(items), items=items
    )


# --------------------------------------------------------------------------- #
# /skills/{skill_id}/readiness                                                 #
# --------------------------------------------------------------------------- #
@router.get("/{skill_id}/readiness", response_model=SkillReadinessResponse)
async def skill_readiness(
    skill_id: str,
    tenant: str = Depends(resolve_tenant),
) -> SkillReadinessResponse:
    """Readiness facts for one skill — with the honesty rule baked in.

    The measured ``rate`` is the current version's ``eval_score`` **only when it
    was really measured**; otherwise it is ``None`` (rendered「—」), never ``0``
    (INC46 红线 4). ``evaluated`` is the number of measured evaluations the rate
    is based on (``0`` or ``1`` here) so a consumer can distinguish "unmeasured"
    from "measured as zero". A cross-tenant / unknown skill ⇒ ``404``.
    """
    tenant = _tenant_or_403(tenant)
    repo = get_skill_repository()
    skill = await _owned_skill(repo, tenant, skill_id)
    version = await _current_version(repo, tenant, skill)

    eval_score = getattr(version, "eval_score", None) if version is not None else None
    measured = isinstance(eval_score, (int, float)) and not isinstance(eval_score, bool)
    evaluated = 1 if measured else 0
    # 未测量 ⇒ None（绝不写 0）；已测量 ⇒ 真实的 eval_score。
    rate: float | None = float(eval_score) if measured else None

    rules = await list_rules(tenant, limit=1)

    checks = [
        SkillReadinessCheck(
            name="has_version",
            ok=version is not None,
            evidence=(
                f"当前版本 {getattr(skill, 'current_version', None)}"
                if version is not None
                else "该技能尚无版本"
            ),
        ),
        SkillReadinessCheck(
            name="has_evaluation",
            ok=measured,
            evidence=(
                f"已录入评估得分 {float(eval_score):.4f}"
                if measured
                else "未测量：当前版本没有评估得分（rate=None，非 0）"
            ),
        ),
        SkillReadinessCheck(
            name="rules_extracted",
            ok=bool(rules),
            evidence=(
                "该租户已抽取规则资产"
                if rules
                else "该租户暂无规则资产（未抽取）"
            ),
        ),
    ]

    return SkillReadinessResponse(
        skill_id=skill_id,
        tenant_id=tenant,
        ready=all(check.ok for check in checks),
        evaluated=evaluated,
        rate=rate,
        has_version=version is not None,
        current_version=(
            str(getattr(skill, "current_version", "") or "") or None
            if version is not None
            else None
        ),
        checks=checks,
    )
