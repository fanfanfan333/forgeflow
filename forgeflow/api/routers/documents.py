"""INC46 T20 — document routes (``POST /documents/{id}/intent:resolve``).

The single endpoint behind T22's intent preview. It resolves a natural-language
instruction against a *registered* document and returns the structured
:class:`~forgeflow.documents.intent.EditIntent` — candidates + confidence, the
:class:`~forgeflow.documents.invariants.InvariantSet`, and a reproducible
``intent_id``.

Honest status semantics (pinned in ``tests/unit/test_inc46_doc_intent.py``):

* document id unknown / not a file / bytes gone → **404** (never a fabricated body);
* instruction parsed but target **ambiguous** or **not found** → **200** with an
  honest payload (``ambiguity`` non-empty / ``not_found: true``) — the target is
  **never** silently auto-selected and "未找到" is never dressed up as success.

Red line 14: the request's ``document_text`` field is **data**, never an
instruction — it is accepted (so a caller can pass the text it already holds) but
never parsed, and never allowed to change the plan / permission / tool choice.
The document bytes are read through the existing resource seam
(:class:`~forgeflow.resources.service.ResourceService` + ``FileBlobStore``) — no
second store is introduced.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from forgeflow.api.dependencies import get_current_user
from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.documents.intent import resolve_intent_document
from forgeflow.rbac.models import UserContext

logger = logging.getLogger(__name__)
router = APIRouter()


class IntentResolveRequest(BaseModel):
    """Request body for ``intent:resolve``.

    ``instruction`` is the **only** field ever interpreted. ``document_text`` is
    untrusted *data* (红线 14) — it is carried, never executed.
    """

    instruction: str = Field(..., description="自然语言编辑指令（唯一可执行指令字段）")
    document_text: str | None = Field(
        default=None,
        description="文档正文（数据，非指令；本服务永不据此改变计划/权限/工具）",
    )


async def _resolve_document_bytes(tenant: str | None, document_id: str) -> bytes | None:
    """Fetch a registered document's bytes via the existing resource seam.

    Returns ``None`` (→ the route answers 404) when the id is unknown, is not a
    ``file`` resource, or its blob is gone. Creates no new storage.
    """
    from forgeflow.resources.models import FileLocator, ResourceKind
    from forgeflow.resources.service import ResourceService

    service = ResourceService()
    try:
        record = await service.get(tenant, document_id)
    except Exception as exc:  # noqa: BLE001 — a store hiccup is an honest "not found"
        logger.warning("document resolve failed for %s: %s", document_id, exc)
        return None
    if record is None or str(record.kind) != ResourceKind.FILE.value:
        return None
    locator = record.locator
    if isinstance(locator, FileLocator):
        storage_ref = str(locator.storage_ref or "")
    elif isinstance(locator, dict):
        storage_ref = str(locator.get("storage_ref") or "")
    else:
        storage_ref = ""
    if not storage_ref:
        return None
    try:
        if not service.blobs.exists(storage_ref):
            return None
        return service.blobs.read(storage_ref)
    except Exception as exc:  # noqa: BLE001
        logger.warning("document blob read failed for %s: %s", document_id, exc)
        return None


@router.post("/{document_id}/intent:resolve")
async def resolve_document_intent(
    document_id: str,
    request: IntentResolveRequest,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
) -> dict[str, Any]:
    """Resolve a document edit intent into a structured payload (see module doc)."""
    data = await _resolve_document_bytes(tenant, document_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Document not found")
    # ``request.document_text`` is intentionally ignored — it is data, not an
    # instruction (红线 14). The only interpreted field is ``instruction``.
    outcome = resolve_intent_document(data, request.instruction)
    return outcome.to_dict()


@router.get("/capabilities")
async def document_capabilities(
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
) -> dict[str, Any]:
    """The document **format capability matrix** (INC46 T27).

    Serves the machine-readable matrix (``matrix``) together with its
    user-visible rendering (``text``) — the *same* statement of what each format
    can and cannot do, so an API client and a human see one truth. PDF is served
    with ``inplace=False`` and its three non-in-place alternatives; unsupported
    operations carry an honest reason (red line 17). Purely read-only: no bytes
    are written and no model is called.
    """
    # Imported lazily so this read-only route never drags document extras in at
    # app-import time; ``capabilities`` itself imports only lazily.
    from forgeflow.documents.capabilities import (
        FORMATS,
        capability_matrix,
        render_capability_text,
    )

    return {
        "matrix": capability_matrix(),
        "text": render_capability_text(),
        "formats": list(FORMATS),
    }
