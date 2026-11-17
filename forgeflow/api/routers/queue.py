"""Queue routes — ``POST /queue/tasks`` enqueues a run request for a
worker instead of running it synchronously (``POST /tasks``).

RBAC mirrors ``/runs`` (``rbac/policies.py::ROUTE_PERMISSION_MAP``):
enqueue needs ``execute:workflows``, reads need ``read:workflows``.
Enqueue is idempotent on ``(tenant, idempotency_key)``: a re-delivered
request returns the existing row with 200, never a second queued run.

A5 slice 2 adds ``/queue/schedules`` — cron schedules that fire
into the same queue. Create/update/delete need ``execute:workflows``
(the same write grant as enqueue); reads need ``read:workflows``.

A5 slice 4 adds ``GET /queue/workers`` — worker liveness from the
``worker_heartbeats`` table. Heartbeats are platform-wide (one
worker serves all tenants, like the claim operation), so the list
is deliberately NOT tenant-filtered; the route still requires the
``read:workflows`` grant.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from forgeflow.api.dependencies import get_current_user
from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.queue.heartbeats import get_heartbeat_store
from forgeflow.queue.schedules import (
    create_schedule,
    get_schedule_store,
    update_schedule,
)
from forgeflow.queue.service import enqueue_task
from forgeflow.queue.store import get_task_queue_store
from forgeflow.rbac.models import UserContext

logger = logging.getLogger(__name__)
router = APIRouter()


class EnqueueRequest(BaseModel):
    """``POST /queue/tasks`` body — the ``POST /tasks`` declaration
    plus queue options."""

    intent: str = Field(..., min_length=1, description="Task intent (required)")
    title: str = Field(default="", description="Short title for the run")
    workflow_type: str = Field(default="generic", description="Workflow type")
    context: dict[str, Any] = Field(default_factory=dict, description="Task inputs")
    idempotency_key: str | None = Field(
        default=None,
        description="Client key: a replay returns the existing row (generated when omitted)",
    )
    max_attempts: int = Field(default=3, ge=1, le=10, description="Tries before dead")


class QueuedTaskView(BaseModel):
    """One queued task (``QueuedTask.to_dict`` shape)."""

    id: str
    tenant_id: str
    owner_id: str
    task: dict[str, Any]
    idempotency_key: str
    status: str
    attempts: int
    max_attempts: int
    next_attempt_at: datetime | None = None
    claimed_by: str | None = None
    lease_expires_at: datetime | None = None
    last_error: str | None = None
    run_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@router.post("/tasks", response_model=QueuedTaskView)
async def enqueue_queued_task(
    request: EnqueueRequest,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """Enqueue a task for a worker. Idempotent: a replay of the same
    ``idempotency_key`` under this tenant returns the existing row
    (200), it does not enqueue a second run."""
    store = get_task_queue_store()
    try:
        row = await asyncio.to_thread(
            enqueue_task,
            store,
            tenant_id=tenant,
            owner_id=user.user_id,
            task={
                "intent": request.intent,
                "title": request.title,
                "workflow_type": request.workflow_type,
                "context": request.context,
            },
            idempotency_key=request.idempotency_key,
            max_attempts=request.max_attempts,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    logger.info(
        "queue enqueue | tenant=%s owner=%s task=%s status=%s",
        tenant,
        user.user_id,
        row.id,
        row.status,
    )
    return QueuedTaskView(**row.to_dict())


@router.get("/tasks", response_model=list[QueuedTaskView])
async def list_queued_tasks(
    limit: int = Query(default=20, ge=1, le=100),
    status: str | None = Query(default=None, description="Filter by status"),
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """List this tenant's queued tasks, newest first."""
    store = get_task_queue_store()
    rows = await asyncio.to_thread(store.list_tasks, tenant, limit=limit, status=status)
    return [QueuedTaskView(**row.to_dict()) for row in rows]


@router.get("/tasks/{task_id}", response_model=QueuedTaskView)
async def get_queued_task(
    task_id: str,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """One queued task. Another tenant's row is a 404 (never a
    cross-tenant leak), like the runs router's owner check."""
    store = get_task_queue_store()
    row = await asyncio.to_thread(store.get, tenant, task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Queued task not found")
    return QueuedTaskView(**row.to_dict())


# --------------------------------------------------------------------------- #
# A5 slice 2 — cron schedules                                                 #
# --------------------------------------------------------------------------- #


class ScheduleCreateRequest(BaseModel):
    """``POST /queue/schedules`` body — a cron schedule whose fires
    enqueue the declared task."""

    name: str = Field(..., min_length=1, description="Schedule name (required)")
    cron_expr: str = Field(
        ..., description="5-field cron expression (minute hour day month weekday)"
    )
    timezone: str = Field(default="UTC", description="IANA timezone the cron is evaluated in")
    intent: str = Field(..., min_length=1, description="Task intent (required)")
    title: str = Field(default="", description="Short title for the run")
    workflow_type: str = Field(default="generic", description="Workflow type")
    context: dict[str, Any] = Field(default_factory=dict, description="Task inputs")
    enabled: bool = Field(default=True, description="Disabled schedules never fire")


class ScheduleUpdateRequest(BaseModel):
    """``PATCH /queue/schedules/{id}`` body — all fields optional.
    Changing ``cron_expr`` or ``timezone`` recomputes ``next_fire_at``
    from now."""

    name: str | None = Field(default=None, min_length=1)
    cron_expr: str | None = Field(default=None)
    timezone: str | None = Field(default=None)
    intent: str | None = Field(default=None, min_length=1)
    title: str | None = Field(default=None)
    workflow_type: str | None = Field(default=None)
    context: dict[str, Any] | None = Field(default=None)
    enabled: bool | None = Field(default=None)


class ScheduleView(BaseModel):
    """One schedule (``Schedule.to_dict`` shape)."""

    id: str
    tenant_id: str
    owner_id: str
    name: str
    cron_expr: str
    timezone: str
    task: dict[str, Any]
    enabled: bool
    next_fire_at: datetime | None = None
    last_fire_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@router.post("/schedules", response_model=ScheduleView)
async def create_queued_schedule(
    request: ScheduleCreateRequest,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """Create a cron schedule. The first fire is the next cron time
    after now; each fire enqueues the declared task (exactly once
    per fire time, via a deterministic idempotency key)."""
    store = get_schedule_store()
    try:
        row = await asyncio.to_thread(
            create_schedule,
            store,
            tenant_id=tenant,
            owner_id=user.user_id,
            name=request.name,
            cron_expr=request.cron_expr,
            timezone=request.timezone,
            task={
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
        "schedule create | tenant=%s owner=%s schedule=%s cron=%s tz=%s",
        tenant,
        user.user_id,
        row.id,
        row.cron_expr,
        row.timezone,
    )
    return ScheduleView(**row.to_dict())


@router.get("/schedules", response_model=list[ScheduleView])
async def list_queued_schedules(
    limit: int = Query(default=20, ge=1, le=100),
    enabled: bool | None = Query(default=None, description="Filter by enabled"),
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """List this tenant's schedules, newest first."""
    store = get_schedule_store()
    rows = await asyncio.to_thread(store.list_schedules, tenant, limit=limit, enabled=enabled)
    return [ScheduleView(**row.to_dict()) for row in rows]


@router.get("/schedules/{schedule_id}", response_model=ScheduleView)
async def get_queued_schedule(
    schedule_id: str,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """One schedule. Another tenant's row is a 404 (never a
    cross-tenant leak), like the queued-task read."""
    store = get_schedule_store()
    row = await asyncio.to_thread(store.get, tenant, schedule_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return ScheduleView(**row.to_dict())


@router.patch("/schedules/{schedule_id}", response_model=ScheduleView)
async def update_queued_schedule(
    schedule_id: str,
    request: ScheduleUpdateRequest,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """Patch a schedule. Changing ``cron_expr`` or ``timezone``
    recomputes ``next_fire_at`` from now; patching only the task or
    ``enabled`` leaves the next fire untouched."""
    store = get_schedule_store()
    task = None
    if (
        request.intent is not None
        or request.title is not None
        or request.workflow_type is not None
        or request.context is not None
    ):
        current = await asyncio.to_thread(store.get, tenant, schedule_id)
        if current is None:
            raise HTTPException(status_code=404, detail="Schedule not found")
        task = {
            "intent": request.intent
            if request.intent is not None
            else current.task.get("intent", ""),
            "title": request.title if request.title is not None else current.task.get("title", ""),
            "workflow_type": (
                request.workflow_type
                if request.workflow_type is not None
                else current.task.get("workflow_type", "generic")
            ),
            "context": request.context
            if request.context is not None
            else current.task.get("context", {}),
        }
    try:
        row = await asyncio.to_thread(
            update_schedule,
            store,
            tenant_id=tenant,
            schedule_id=schedule_id,
            name=request.name,
            cron_expr=request.cron_expr,
            timezone=request.timezone,
            task=task,
            enabled=request.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return ScheduleView(**row.to_dict())


@router.delete("/schedules/{schedule_id}", status_code=204)
async def delete_queued_schedule(
    schedule_id: str,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """Delete a schedule. Another tenant's row is a 404 (never a
    cross-tenant delete). Already-fired queue rows are unaffected."""
    store = get_schedule_store()
    deleted = await asyncio.to_thread(store.delete, tenant, schedule_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Schedule not found")
    logger.info("schedule delete | tenant=%s schedule=%s", tenant, schedule_id)
    return None


# --------------------------------------------------------------------------- #
# A5 slice 4 — worker liveness                                               #
# --------------------------------------------------------------------------- #


class WorkerHeartbeatView(BaseModel):
    """One worker process's latest heartbeat."""

    worker_id: str
    hostname: str | None = None
    started_at: datetime | None = None
    last_beat_at: datetime | None = None
    last_report: dict[str, Any]
    updated_at: datetime | None = None
    stale: bool = False


@router.get("/workers", response_model=list[WorkerHeartbeatView])
async def list_queue_workers(
    limit: int = Query(default=20, ge=1, le=100),
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """List worker heartbeats, newest beat first.

    The list is platform-wide, not tenant-filtered: one worker
    serves all tenants (the same reason the claim operation is
    cross-tenant). ``stale`` is true when the worker has not beaten
    within 3x its poll interval — that worker is down.
    """
    store = get_heartbeat_store()
    now = datetime.now(UTC)
    rows = await asyncio.to_thread(store.list_heartbeats, limit=limit)
    return [
        WorkerHeartbeatView(
            **row.to_dict(),
            stale=row.is_stale(now),
        )
        for row in rows
    ]
