"""Resource Center routes (INC25 W1, P0-1 / P0-2 / P0-5 / P0-7).

Five independently-addressable registration entry points (one per kind) plus a
list / detail / preview surface:

  * ``POST /resources/files``           — multipart byte upload (real bytes)
  * ``POST /resources/database``        — register a table
  * ``POST /resources/code``            — register a code source (repo / path / zip)
  * ``POST /resources/knowledge_base``  — register a knowledge base
  * ``POST /resources/api``             — register an API connector
  * ``GET  /resources``                 — list (optional ``kind`` filter)
  * ``GET  /resources/limits``          — upload limit + supported extensions
  * ``GET  /resources/{id}``            — detail
  * ``GET  /resources/{id}/preview``    — first-N-rows / lines preview

HTTP semantics are honest (P0-5):
  * over-limit upload → **413** with the actual byte count + the limit in ``detail``;
  * unsupported type  → **400** with a verbatim reason, **no** "已解析" entry;
  * a parse-capability gap is a server-side *degradation*, not an error: the
    resource is stored with status ``metadata_only`` / ``ignored`` and the
    request stays **2xx** (never 5xx).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile

from forgeflow.api.dependencies import get_current_user
from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.resource_schemas import (
    ApiResourceRequest,
    CodeResourceRequest,
    DatabaseResourceRequest,
    KnowledgeBaseResourceRequest,
    ResourceDeleteResponse,
    ResourceLimitsResponse,
    ResourceListResponse,
    ResourcePreviewResponse,
    ResourceResponse,
)
from forgeflow.rbac.models import UserContext
from forgeflow.resources.code_sources import CODE_SOURCE_TYPES
from forgeflow.resources.models import ResourceRecord
from forgeflow.resources.service import (
    ResourceService,
    ResourceTooLargeError,
    UnsupportedResourceTypeError,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _service() -> ResourceService:
    return ResourceService()


def _to_response(record: ResourceRecord) -> ResourceResponse:
    return ResourceResponse(**record.to_dict())


@router.get("", response_model=ResourceListResponse)
async def list_resources(
    kind: str | None = Query(None, description="Optional resource-kind filter"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    tenant: str = Depends(resolve_tenant),
):
    """List the tenant's registered resources (newest first)."""
    rows = await _service().list(tenant, kind=kind, limit=limit, offset=offset)
    return ResourceListResponse(total=len(rows), items=[_to_response(r) for r in rows])


@router.post("/files", response_model=ResourceResponse, status_code=201)
async def register_file(
    file: UploadFile = File(...),
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """Register an uploaded file (real bytes, real summary)."""
    data = await file.read()
    try:
        record = await _service().register_file(
            tenant, name=file.filename or "upload", data=data, created_by=user.user_id
        )
    except ResourceTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except UnsupportedResourceTypeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _to_response(record)


@router.post("/database", response_model=ResourceResponse, status_code=201)
async def register_database(
    request: DatabaseResourceRequest,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """Register a database table resource."""
    try:
        record = await _service().register_database(
            tenant, table=request.table, created_by=user.user_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _to_response(record)


@router.post("/code", response_model=ResourceResponse, status_code=201)
async def register_code(
    request: CodeResourceRequest,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """Register a code source (GitHub/GitLab repo, local path, or ZIP)."""
    source_type = (request.source_type or "").strip().lower()
    if source_type not in CODE_SOURCE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"不支持的代码来源类型：{request.source_type!r}；"
                f"可选：{', '.join(CODE_SOURCE_TYPES)}"
            ),
        )
    source: dict[str, Any] = {
        "source_type": source_type,
        "identifier": request.identifier or request.repository or request.path,
        "branch": request.branch,
        "filename": request.filename,
        "zip_base64": request.zip_base64,
    }
    record = await _service().register_code(tenant, source=source, created_by=user.user_id)
    return _to_response(record)


@router.post("/knowledge_base", response_model=ResourceResponse, status_code=201)
async def register_knowledge_base(
    request: KnowledgeBaseResourceRequest,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """Register a knowledge-base resource."""
    try:
        record = await _service().register_knowledge_base(
            tenant, kb_id=request.kb_id, scope=request.scope, created_by=user.user_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _to_response(record)


@router.post("/api", response_model=ResourceResponse, status_code=201)
async def register_api(
    request: ApiResourceRequest,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """Register an API connector resource."""
    try:
        record = await _service().register_api(
            tenant, connector=request.connector, base_url=request.base_url, created_by=user.user_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _to_response(record)


@router.get("/limits", response_model=ResourceLimitsResponse)
async def get_resource_limits(tenant: str = Depends(resolve_tenant)):
    """Upload limits + supported extensions, from the single source of truth.

    INC26 T01 / P0-2 — the front-end's pre-check driver. Values are resolved
    **live** by ``ResourceService.limits()`` (never a frozen literal). Declared
    **before** ``GET /resources/{resource_id}`` so ``/limits`` is not swallowed
    by the path parameter. No new RBAC entry: it inherits the existing
    ``("GET", "/resources")`` → ``read:skills`` grant via longest-prefix match.
    """
    return ResourceLimitsResponse(**_service().limits())


@router.get("/{resource_id}", response_model=ResourceResponse)
async def get_resource(resource_id: str, tenant: str = Depends(resolve_tenant)):
    """Fetch one resource's detail (tenant-scoped)."""
    record = await _service().get(tenant, resource_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Resource not found")
    return _to_response(record)


@router.delete("/{resource_id}", response_model=ResourceDeleteResponse)
async def delete_resource(resource_id: str, tenant: str = Depends(resolve_tenant)):
    """Delete one of the tenant's resources (INC40).

    Status semantics:
      * exists → **200** ``{"deleted": true, "resource_id": ...}`` (a real JSON
        body — never 204, see ``frontend/src/api/client.ts::request``);
      * missing **or** owned by another tenant → **404** (no existence leak; the
        same口径 as ``GET /resources/{id}``, and it satisfies "cross-tenant
        delete must 404" automatically).

    404 rather than an "idempotent 204": it aligns with the existing
    ``GET /resources/{id}`` semantics, and lets the front-end tell "deleted"
    apart from "never existed / not yours" instead of green-lighting a
    cross-tenant no-op. The underlying record is removed but its blob is **not**
    (content-addressed + de-duplicated — see ``ResourceService.delete``).
    """
    deleted = await _service().delete(tenant, resource_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Resource not found")
    return ResourceDeleteResponse(deleted=True, resource_id=resource_id)


@router.get("/{resource_id}/preview", response_model=ResourcePreviewResponse)
async def preview_resource(
    resource_id: str,
    n: int = Query(20, ge=1, le=500),
    tenant: str = Depends(resolve_tenant),
):
    """Preview a resource's content (first N rows / lines)."""
    data = await _service().preview(tenant, resource_id, n=n)
    if data.get("note") == "资源不存在":
        raise HTTPException(status_code=404, detail="Resource not found")
    return ResourcePreviewResponse(**data)
