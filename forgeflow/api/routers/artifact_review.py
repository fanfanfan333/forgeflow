"""INC46 T22 — 文档 Diff 预览与确认路由（``/artifacts``）。

规格（任务书 API）
------------------
  * ``GET  /artifacts/{id}/versions/{v}/diff``    —— diff 预览（段落 / run / 单元格）；
  * ``POST /artifacts/{id}/versions/{v}/approve`` —— 人工确认，产出 ``committed`` 版本；
  * ``POST /artifacts/{id}/versions/{v}/reject``  —— 拒绝（原因写入 T16）；
  * ``GET  /artifacts/{id}/versions/{v}/tracked`` —— 可选 ``.tracked.docx``（修订模式）。

为让上面四个动作**真的可复算**（而不是对着一个不存在的东西 approve），本路由另加两个
**加性**入口（书面差异，均在白名单内的本文件里）：

  * ``POST /artifacts/{id}/versions``              —— 由「原文 + 编辑」产出 ``pending``
    版本 + diff + 区间外变化，并落一条 ``artifact_reviews``（拿到真实 ``approval_id``）；
  * ``GET  /artifacts/{id}/versions/{v}/content``  —— 取回该版本的制品字节（approve 后
    即 ``.edited.docx``）。

纪律
----
* 红线 3：区间外变化 / 非 pending 版本 / 跨租户 / 无租户 —— 一律 fail-closed，绝不折算成通过。
* 红线 4：未测量一律 ``None``（``out_of_region=None`` 表示「未测量」，与「空列表 = 干净」
  是两个不同事实）。
* 红线 5：租户先行，resolve 不了即 403；跨租户即 403。
* 红线 11：未经 approve 不产生 ``committed`` 版本。
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.documents.diff import diff_documents
from forgeflow.documents.docx_edit import apply_edits
from forgeflow.documents.docx_inspect import DocxInspectionError
from forgeflow.documents.review_store import (
    ArtifactNotFound,
    CrossTenantArtifact,
    OutOfRegionBlocked,
    ReviewNotFound,
    VersionNotPending,
    get_artifact_review_store,
)
from forgeflow.documents.tracked_changes import build_tracked_docx, validate_tracked_docx
from forgeflow.skills.errors import GovernanceError
from forgeflow.skills.tenant_scope import require_tenant

logger = logging.getLogger(__name__)
router = APIRouter()

_DOCX_MEDIA = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


# --------------------------------------------------------------------------- #
# Request / response models                                                    #
# --------------------------------------------------------------------------- #
class Region(BaseModel):
    """The edit's target region, in source-document paragraph indices ``[start, end)``."""

    start: int
    end: int


class CreateVersionRequest(BaseModel):
    """Produce a ``pending`` version + diff from a source document + edits."""

    run_id: str
    edits: list[dict[str, Any]] = Field(default_factory=list)
    #: The source document bytes (base64). Required for v1 (no prior version).
    source_b64: str | None = None
    #: Use a stored version's bytes as the source instead of ``source_b64``.
    base_version: int | None = None
    #: The target region; when given, out-of-region changes are measured + block approve.
    region: Region | None = None
    instruction: str | None = None
    format: str = "docx"


class ApproveRequest(BaseModel):
    actor: str | None = None
    override: bool = False
    override_reason: str | None = None


class RejectRequest(BaseModel):
    actor: str | None = None
    reason: str | None = None


class DiffPreview(BaseModel):
    artifact_id: str
    version: int
    state: str
    base_version: int | None = None
    approval_id: str | None = None
    run_id: str | None = None
    diff: dict[str, Any] | None = None
    #: ``None`` ⇒ 未测量；``[]`` ⇒ 已测量且干净；非空 ⇒ 阻断 approve。
    out_of_region: list[dict[str, Any]] | None = None
    blocked: bool = False
    can_approve: bool = False
    tracked_available: bool = False


def _tenant_or_403(tenant: str | None) -> str:
    """Fail closed on an unresolved tenant (红线 5)."""
    try:
        return require_tenant(tenant)
    except GovernanceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


def _map_error(exc: Exception) -> HTTPException:
    if isinstance(exc, CrossTenantArtifact):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (ArtifactNotFound, ReviewNotFound)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (VersionNotPending, OutOfRegionBlocked)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


def _preview(tenant: str, artifact_id: str, version: int) -> DiffPreview:
    """Project a stored version onto the wire preview (fail-closed on missing)."""
    store = get_artifact_review_store()
    try:
        record = _require_version(store, tenant, artifact_id, version)
    except Exception as exc:  # noqa: BLE001 — 404 (unknown) vs 403 (foreign tenant)
        raise _map_error(exc) from exc
    blocked = bool(record.out_of_region)
    return DiffPreview(
        artifact_id=artifact_id,
        version=version,
        state=record.state,
        base_version=record.base_version,
        approval_id=record.review_id,
        run_id=record.run_id,
        diff=record.diff,
        out_of_region=record.out_of_region,
        blocked=blocked,
        can_approve=(record.state == "pending" and not blocked),
        tracked_available=record.base_version is not None,
    )


def _require_version(store: Any, tenant: str, artifact_id: str, version: int) -> Any:
    """Tenant-scoped version lookup that distinguishes 404 from 403."""
    record = store.get_version(tenant, artifact_id, version)
    if record is not None:
        return record
    # The version may exist under another tenant ⇒ 403 (never a shadow read).
    if store._load_version_any(artifact_id, version) is not None:  # noqa: SLF001
        raise CrossTenantArtifact(artifact_id)
    raise ArtifactNotFound(f"{artifact_id}@v{version}")


# --------------------------------------------------------------------------- #
# Routes                                                                       #
# --------------------------------------------------------------------------- #
@router.post("/{artifact_id}/versions", response_model=DiffPreview)
async def create_version(
    artifact_id: str,
    body: CreateVersionRequest,
    tenant: str = Depends(resolve_tenant),
) -> DiffPreview:
    """Create a ``pending`` version + diff from a source document + edits."""
    tenant_id = _tenant_or_403(tenant)
    store = get_artifact_review_store()

    try:
        if body.base_version is not None:
            source = store.read_content(tenant_id, artifact_id, body.base_version)
        elif body.source_b64:
            source = base64.b64decode(body.source_b64)
        else:
            raise HTTPException(status_code=400, detail="需要 source_b64 或 base_version")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — map to a real HTTP status
        raise _map_error(exc) from exc

    try:
        edited, _changes = apply_edits(source, body.edits)
    except (DocxInspectionError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"编辑失败：{exc}") from exc

    try:
        diff = diff_documents(source, edited, fmt=body.format)
    except DocxInspectionError as exc:
        raise HTTPException(status_code=400, detail=f"无法计算 diff：{exc}") from exc

    out_of_region = (
        diff.out_of_region(body.region.start, body.region.end) if body.region is not None else None
    )

    try:
        version, review = store.create_pending_version(
            tenant_id,
            artifact_id=artifact_id,
            content=edited,
            fmt=body.format,
            diff=diff.to_dict(),
            out_of_region=out_of_region,
            base_version=body.base_version,
            run_id=body.run_id,
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc

    logger.info(
        "pending version created | tenant=%s artifact=%s v=%d approval=%s",
        tenant_id, artifact_id, version.version, review.approval_id,
    )
    return DiffPreview(
        artifact_id=artifact_id,
        version=version.version,
        state=version.state,
        base_version=version.base_version,
        approval_id=review.approval_id,
        run_id=version.run_id,
        diff=version.diff,
        out_of_region=version.out_of_region,
        blocked=bool(version.out_of_region),
        can_approve=(version.state == "pending" and not version.out_of_region),
        tracked_available=version.base_version is not None,
    )


@router.get("/{artifact_id}/versions/{version}/diff", response_model=DiffPreview)
async def get_diff(
    artifact_id: str,
    version: int,
    tenant: str = Depends(resolve_tenant),
) -> DiffPreview:
    """The diff preview for a stored version (paragraph / run / table-cell)."""
    tenant_id = _tenant_or_403(tenant)
    return _preview(tenant_id, artifact_id, version)


@router.post("/{artifact_id}/versions/{version}/approve", response_model=DiffPreview)
async def approve_version(
    artifact_id: str,
    version: int,
    body: ApproveRequest,
    tenant: str = Depends(resolve_tenant),
) -> DiffPreview:
    """Commit a pending version after a human approves its diff.

    ``409`` when the version is not pending, or when out-of-region changes exist
    and no explicit override was supplied (红线 3 / 11).
    """
    tenant_id = _tenant_or_403(tenant)
    store = get_artifact_review_store()
    try:
        record, review = store.approve(
            tenant_id,
            artifact_id,
            version,
            actor=body.actor,
            override=body.override,
            override_reason=body.override_reason,
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc
    preview = _preview(tenant_id, artifact_id, version)
    logger.info(
        "version approved | tenant=%s artifact=%s v=%d approval=%s",
        tenant_id, artifact_id, record.version, review.approval_id,
    )
    return preview


@router.post("/{artifact_id}/versions/{version}/reject", response_model=DiffPreview)
async def reject_version(
    artifact_id: str,
    version: int,
    body: RejectRequest,
    tenant: str = Depends(resolve_tenant),
) -> DiffPreview:
    """Reject a pending version; the reason (if any) enters T16 feedback_events."""
    tenant_id = _tenant_or_403(tenant)
    store = get_artifact_review_store()
    try:
        store.reject(tenant_id, artifact_id, version, actor=body.actor, reason=body.reason)
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc
    preview = _preview(tenant_id, artifact_id, version)
    logger.info(
        "version rejected | tenant=%s artifact=%s v=%d reason=%s",
        tenant_id, artifact_id, version, body.reason,
    )
    return preview


@router.get("/{artifact_id}/versions/{version}/tracked")
async def get_tracked(
    artifact_id: str,
    version: int,
    tenant: str = Depends(resolve_tenant),
) -> Response:
    """Render the version as a Word **tracked-changes** DOCX (base → this version).

    Requires a ``base_version``; a v1 with no base has nothing to track ⇒ ``409``.
    """
    tenant_id = _tenant_or_403(tenant)
    store = get_artifact_review_store()
    try:
        record = store.get_version(tenant_id, artifact_id, version)
        if record is None:
            raise ArtifactNotFound(f"{artifact_id}@v{version}")
        if record.base_version is None:
            raise HTTPException(
                status_code=409, detail="该版本无 base_version，无法生成修订模式文档"
            )
        new_bytes = store.read_content(tenant_id, artifact_id, version)
        old_bytes = store.read_content(tenant_id, artifact_id, record.base_version)
        tracked = build_tracked_docx(old_bytes, new_bytes)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc

    validation = validate_tracked_docx(tracked)
    if not validation["valid"]:
        # A structurally invalid tracked document is never handed back (红线 3).
        raise HTTPException(status_code=500, detail=f"修订模式产物未通过结构校验：{validation['errors']}")

    return Response(
        content=tracked,
        media_type=_DOCX_MEDIA,
        headers={"content-disposition": f'attachment; filename="{artifact_id}.tracked.docx"'},
    )


@router.get("/{artifact_id}/versions/{version}/content")
async def get_content(
    artifact_id: str,
    version: int,
    tenant: str = Depends(resolve_tenant),
) -> Response:
    """Return the version's deliverable bytes (the ``.edited.docx`` after approve)."""
    tenant_id = _tenant_or_403(tenant)
    store = get_artifact_review_store()
    try:
        payload = store.read_content(tenant_id, artifact_id, version)
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc
    return Response(
        content=payload,
        media_type=_DOCX_MEDIA,
        headers={"content-disposition": f'attachment; filename="{artifact_id}.edited.docx"'},
    )
