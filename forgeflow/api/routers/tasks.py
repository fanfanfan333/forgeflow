"""Task routes — ``POST /tasks`` creates a Run and drives the closed loop."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field

from forgeflow.api.dependencies import get_current_user
from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.hub_schemas import RunHandleResponse, TaskCreateRequest
from forgeflow.governance.policy_engine import evaluate_task_entry
from forgeflow.rbac.models import UserContext
from forgeflow.runtime.attachments import (
    AttachmentInput,
    AttachmentTooLargeError,
    prepare_attachments,
)
from forgeflow.runtime.orchestrator import RequestContext, TaskCreate, run_task

logger = logging.getLogger(__name__)
router = APIRouter()


class TaskCreateRequestWithAttachments(TaskCreateRequest):
    """``POST /tasks`` body = the hub request + optional C4 attachments.

    Declared here (rather than widening ``hub_schemas``) so the additive
    ``attachments`` field travels with the route that implements it (INC2-24);
    the body stays backward-compatible — ``attachments`` defaults to empty.
    """

    attachments: list[AttachmentInput] = Field(default_factory=list)


@router.post("", response_model=RunHandleResponse)
async def create_task(
    request: TaskCreateRequestWithAttachments,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """Create a task, risk-grade it, then run it to a terminal state.

    Returns the RunHandle; the run's experience is extracted automatically
    (jump ②) and its events are streamed on ``GET /runs/{id}/events``.

    INC2-24: any attached PDF/image is pre-processed and its text injected into
    the task intent before the run starts. Optional multimodal dependencies
    degrade to "attachment ignored" rather than failing the request.
    """
    decision = await evaluate_task_entry(
        user.user_id, user.role, request.intent, tenant_id=tenant
    )
    if not decision.allowed:
        raise HTTPException(status_code=403, detail=decision.reason)
    if decision.requires_approval:
        raise HTTPException(
            status_code=403,
            detail=f"high-risk task requires human approval: {decision.reason}",
        )

    try:
        prepared = await prepare_attachments(request.attachments, intent=request.intent)
    except AttachmentTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc

    ctx = RequestContext(tenant_id=tenant, user_id=user.user_id, role=user.role)
    task = TaskCreate(
        intent=prepared.intent,
        title=request.title or request.intent[:60],
        workflow_type=request.workflow_type,
        context={**request.context, **prepared.context},
    )
    try:
        handle = await run_task(task, ctx)
    except Exception as exc:  # noqa: BLE001 — surface a clean 500
        logger.exception("task run failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return RunHandleResponse(**handle.to_dict())

