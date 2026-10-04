"""INC46 T17 — 独立 Golden 回归集路由（``/eval``）。

  * ``GET  /eval/golden-sets`` — 列出租户的 golden 集（只读）。
  * ``POST /eval/golden-sets`` — **admin-only** 人工导入一个 golden 集；``provenance``
    必填，且只能是 ``human_curated`` / ``customer_approved_export``。
  * ``POST /eval/runs``        — 对一个候选跑一次 golden 回归（产生 ``golden_run_id``）。

诚实语义
--------
* **admin-only（红线：来源必须真实）**：写入门在两层 fail-closed —— 路由层要求
  ``role == "admin"``（否则 403），存储层 ``assert_can_write_golden`` 再拒一次；
  evolution 的服务账号（``service``）写 ``golden_*`` 一律拒绝。
* **未测量 ⇒ None（红线 4）**：``golden_runs.passed`` 在无基线时是 ``None``，读回也是
  ``None``，绝不冒充 ``False``/``0``。
* **无冻结集 ⇒ 诚实 skip**：``POST /eval/runs`` 在没有冻结集时返回
  ``ran=False, allowed=None``（不编造通过）。
* **租户隔离（红线 5）**：每个读写都以解析出的租户为第一参数，未解析租户 fail-closed。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from forgeflow.api.dependencies import get_current_user
from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.evaluation.golden_registry import (
    ALLOWED_PROVENANCE,
    GoldenCase,
    GoldenSet,
    ProvenanceError,
    get_golden_registry,
)
from forgeflow.evaluation.golden_regression import run_golden_regression
from forgeflow.rbac.models import UserContext
from forgeflow.skills.errors import GovernanceError

logger = logging.getLogger(__name__)
router = APIRouter()


class GoldenCaseIn(BaseModel):
    """One imported golden case (``source_doc_fingerprint`` is the leakage key)."""

    case_id: str = Field(..., description="用例标识（集合内唯一）")
    source_doc_fingerprint: str = Field(..., description="源文档指纹（sha256:...）")
    run_id: str | None = None
    instruction: str | None = None
    expected_summary: str | None = None


class GoldenSetIn(BaseModel):
    """The import payload for a golden set."""

    name: str = Field(..., description="集合名（租户内唯一）")
    provenance: str = Field(
        ...,
        description="来源：human_curated | customer_approved_export（必填，mined 被拒）",
    )
    cases: list[GoldenCaseIn] = Field(..., min_length=1)
    baseline_metrics: dict[str, float] = Field(default_factory=dict)


class GoldenRunIn(BaseModel):
    """The payload for a manual golden-regression run."""

    skill_id: str = ""
    candidate_id: str = ""
    experience_ids: list[str] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)


def _require_admin(user: UserContext) -> None:
    """Route-level admin gate (the store enforces it again — defence in depth)."""
    if (user.role or "") != "admin":
        raise HTTPException(
            status_code=403,
            detail="仅 admin 可人工导入 golden 评测集（来源必须真实、可溯源）",
        )


@router.get("/golden-sets")
async def list_golden_sets(
    tenant: str = Depends(resolve_tenant),
    user: UserContext = Depends(get_current_user),
) -> dict:
    """List the caller-tenant's golden sets (metadata only; no case bodies)."""
    store = get_golden_registry()
    sets = await store.list_sets(tenant)
    return {"tenant_id": tenant, "sets": [s.to_dict() for s in sets]}


@router.post("/golden-sets")
async def import_golden_set(
    payload: GoldenSetIn,
    tenant: str = Depends(resolve_tenant),
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Import + freeze one golden set (admin only; provenance required + validated)."""
    _require_admin(user)
    cases = [
        GoldenCase(
            case_id=c.case_id,
            source_doc_fingerprint=c.source_doc_fingerprint,
            run_id=c.run_id or "",
            instruction=c.instruction or "",
            expected_summary=c.expected_summary or "",
            provenance=payload.provenance,
        )
        for c in payload.cases
    ]
    gset = GoldenSet(
        tenant_id=tenant,
        name=payload.name,
        provenance=payload.provenance,
        cases=cases,
        baseline_metrics=dict(payload.baseline_metrics),
    )
    try:
        frozen = await get_golden_registry().import_set(
            gset, actor_role=user.role
        )
    except ProvenanceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GovernanceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — e.g. FrozenSetMutation → conflict
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    logger.info(
        "golden set frozen | tenant=%s set=%s cases=%d provenance=%s",
        tenant,
        frozen.set_id,
        frozen.case_count,
        frozen.provenance,
    )
    return frozen.to_dict_with_cases()


@router.post("/runs")
async def create_golden_run(
    payload: GoldenRunIn,
    tenant: str = Depends(resolve_tenant),
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Run one golden regression for a candidate and return its traceable result."""
    try:
        result = await run_golden_regression(
            tenant,
            skill_id=payload.skill_id,
            candidate_id=payload.candidate_id,
            experience_ids=list(payload.experience_ids),
            new_metrics=dict(payload.metrics),
        )
    except GovernanceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return result.to_dict()
