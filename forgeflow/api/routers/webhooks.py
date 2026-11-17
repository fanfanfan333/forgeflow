"""Webhook routes — inbound webhooks that enqueue runs (A5 slice 3).

Two routers live in this module because the two surfaces have
different trust models:

* ``router`` — management (``POST/GET/DELETE /queue/webhooks``):
  normal user session + RBAC, same grants as the other queue
  routes (``execute:workflows`` to write, ``read:workflows`` to
  read). The raw secret is returned exactly once, in the create
  response; list/get carry only the fingerprint.
* ``trigger_router`` — ``POST /webhooks/{id}/trigger``: called by
  external producers, authenticated by the per-webhook HMAC
  signature (the signature is the credential), NOT by a user
  session. The route is intentionally open in
  ``middleware/auth.py`` (``_OPEN_PREFIXES`` — the only such
  addition; the HMAC + expiry + replay claim is the gate) and
  carries no ``ROUTE_PERMISSION_MAP`` entry, so no JWT is required.
  A disabled or unknown webhook is a 404 either way.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from forgeflow.api.dependencies import get_current_user
from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.events.processed_events import get_processed_event_store
from forgeflow.queue.store import get_task_queue_store
from forgeflow.queue.webhooks import (
    WebhookAuthError,
    WebhookNotFound,
    WebhookReplayError,
    WebhookUnavailableError,
    create_webhook,
    get_webhook_store,
    trigger_webhook,
)
from forgeflow.rbac.models import UserContext

logger = logging.getLogger(__name__)
router = APIRouter()
trigger_router = APIRouter()


class WebhookCreateRequest(BaseModel):
    """``POST /queue/webhooks`` body — a webhook + its task template."""

    name: str = Field(..., min_length=1, description="Webhook name (required)")
    intent: str = Field(..., min_length=1, description="Task intent (required)")
    title: str = Field(default="", description="Short title for the run")
    workflow_type: str = Field(default="generic", description="Workflow type")
    context: dict[str, Any] = Field(default_factory=dict, description="Task inputs")
    enabled: bool = Field(default=True, description="Disabled webhooks never trigger")


class WebhookView(BaseModel):
    """One webhook. Never carries the raw secret — only a fingerprint."""

    id: str
    tenant_id: str
    owner_id: str
    name: str
    secret_fingerprint: str
    task_template: dict[str, Any]
    enabled: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None


class WebhookCreateView(WebhookView):
    """Create response: the webhook view plus the raw secret, once.

    The caller must store the secret now — it is never returned again.
    """

    secret: str


class TriggerRequest(BaseModel):
    """``POST /webhooks/{id}/trigger`` body.

    ``task`` overrides/extends the webhook's template (body fields
    win; missing fields fall back to the template). ``event_id`` /
    ``expires_at`` are covered by the signature verbatim — the exact
    strings signed, never re-formatted.
    """

    task: dict[str, Any] | None = Field(
        default=None, description="Task override (merged onto the template)"
    )
    event_id: str = Field(..., min_length=1, description="Producer-assigned unique id")
    expires_at: str = Field(..., min_length=1, description="ISO-8601 expiry (signed)")
    signature: str = Field(..., min_length=1, description="HMAC-SHA256 hex over the trigger")


class TriggerResponse(BaseModel):
    """A verified trigger: the queued run it produced."""

    webhook_id: str
    queued_task_id: str
    status: str


@router.post("/webhooks", response_model=WebhookCreateView)
async def create_queued_webhook(
    request: WebhookCreateRequest,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """Create a webhook. The raw secret is returned here and only
    here — store it now; list/get show only the fingerprint."""
    store = get_webhook_store()
    try:
        row, secret = await asyncio.to_thread(
            create_webhook,
            store,
            tenant_id=tenant,
            owner_id=user.user_id,
            name=request.name,
            task_template={
                "intent": request.intent,
                "title": request.title,
                "workflow_type": request.workflow_type,
                "context": request.context,
            },
            enabled=request.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    logger.info(
        "webhook create | tenant=%s owner=%s webhook=%s name=%s",
        tenant,
        user.user_id,
        row.id,
        row.name,
    )
    return WebhookCreateView(**row.to_dict(), secret=secret)


@router.get("/webhooks", response_model=list[WebhookView])
async def list_queued_webhooks(
    limit: int = Query(default=20, ge=1, le=100),
    enabled: bool | None = Query(default=None, description="Filter by enabled"),
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """List this tenant's webhooks, newest first. No raw secrets —
    only fingerprints."""
    store = get_webhook_store()
    rows = await asyncio.to_thread(store.list_webhooks, tenant, limit=limit, enabled=enabled)
    return [WebhookView(**row.to_dict()) for row in rows]


@router.get("/webhooks/{webhook_id}", response_model=WebhookView)
async def get_queued_webhook(
    webhook_id: str,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """One webhook. Another tenant's row is a 404 (never a
    cross-tenant leak), like the queued-task read."""
    store = get_webhook_store()
    row = await asyncio.to_thread(store.get, tenant, webhook_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return WebhookView(**row.to_dict())


@router.delete("/webhooks/{webhook_id}", status_code=204)
async def delete_queued_webhook(
    webhook_id: str,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """Delete a webhook. Another tenant's row is a 404 (never a
    cross-tenant delete). Already-queued trigger rows are unaffected."""
    store = get_webhook_store()
    deleted = await asyncio.to_thread(store.delete, tenant, webhook_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Webhook not found")
    logger.info("webhook delete | tenant=%s webhook=%s", tenant, webhook_id)
    return None


@trigger_router.post("/{webhook_id}/trigger", response_model=TriggerResponse, status_code=202)
async def trigger_queued_webhook(webhook_id: str, request: TriggerRequest):
    """Trigger a webhook. Authenticated by the per-webhook HMAC
    signature (the signature is the credential) — no user session.

    A disabled or unknown webhook is a 404; a bad signature or an
    expired trigger is a 403; a replayed ``event_id`` is a 409; a
    down replay registry fails closed with a 503. The detail never
    says which signature check failed beyond the status.
    """
    try:
        row = await asyncio.to_thread(
            trigger_webhook,
            get_webhook_store(),
            get_task_queue_store(),
            get_processed_event_store(),
            webhook_id=webhook_id,
            task=request.task,
            event_id=request.event_id,
            expires_at=request.expires_at,
            signature=request.signature,
        )
    except WebhookNotFound:
        raise HTTPException(status_code=404, detail="Webhook not found") from None
    except WebhookAuthError:
        raise HTTPException(
            status_code=403, detail="Trigger signature invalid or expired"
        ) from None
    except WebhookReplayError:
        raise HTTPException(status_code=409, detail="Trigger event already processed") from None
    except WebhookUnavailableError:
        raise HTTPException(
            status_code=503, detail="Replay registry unavailable; trigger refused"
        ) from None
    logger.info(
        "webhook trigger | webhook=%s queued_task=%s status=%s",
        webhook_id,
        row.id,
        row.status,
    )
    return TriggerResponse(webhook_id=webhook_id, queued_task_id=row.id, status=row.status)
