"""Run routes — detail, SSE event stream, and replan (docs §4.1)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from forgeflow.api.dependencies import get_current_user
from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.hub_schemas import (
    ReplanRequest,
    RunDetailResponse,
    RunHandleResponse,
    RunListResponse,
    RunSummaryResponse,
)
from forgeflow.rbac.models import UserContext
from forgeflow.runtime.events import get_event_bus
from forgeflow.runtime.orchestrator import (
    RequestContext,
    TaskCreate,
    get_run_store,
    run_task,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _load_run(run_id: str, tenant: str):
    record = get_run_store().get(run_id)
    # Tenant-scoped: a cross-tenant lookup behaves like "not found" (no leak).
    if record is None or (record.tenant_id not in (tenant, None)):
        raise HTTPException(status_code=404, detail="Run not found")
    return record


@router.get("", response_model=RunListResponse)
async def list_runs(
    limit: int = Query(20, ge=1, le=100),
    tenant: str = Depends(resolve_tenant),
):
    """List the tenant's recent runs (newest first) for the home page."""
    rows = get_run_store().list(tenant, limit=limit)
    items = [
        RunSummaryResponse(
            run_id=r.run_id,
            thread_id=r.thread_id,
            status=r.status,
            outcome=r.outcome,
            intent=r.intent,
            title=r.intent[:60],
            created_at=r.created_at,
            completed_at=r.completed_at,
            experience_id=r.experience_id,
            step_count=len(r.steps),
        )
        for r in rows
    ]
    return RunListResponse(total=len(items), items=items)


@router.get("/{run_id}", response_model=RunDetailResponse)
async def get_run(run_id: str, tenant: str = Depends(resolve_tenant)):
    """Fetch a run's detail (tenant-scoped)."""
    record = _load_run(run_id, tenant)
    return RunDetailResponse(
        run_id=record.run_id,
        thread_id=record.thread_id,
        status=record.status,
        outcome=record.outcome,
        intent=record.intent,
        steps=record.steps,
        errors=record.errors,
        created_at=record.created_at,
        completed_at=record.completed_at,
        experience_id=record.experience_id,
    )


@router.get("/{run_id}/events")
async def run_events(run_id: str, tenant: str = Depends(resolve_tenant)):
    """Server-Sent Events stream for a run's execution.

    Reconnecting replays the run's history (so a finished run still yields its
    steps and a ``[DONE]`` terminator). ``X-Accel-Buffering: no`` disables
    proxy buffering so steps arrive live.
    """
    _load_run(run_id, tenant)
    bus = get_event_bus()
    return StreamingResponse(
        bus.stream(run_id),
        media_type="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@router.post("/{run_id}/replan", response_model=RunHandleResponse)
async def replan_run(
    run_id: str,
    request: ReplanRequest,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """Re-run a task's intent (manual replan trigger)."""
    record = _load_run(run_id, tenant)
    ctx = RequestContext(tenant_id=tenant, user_id=user.user_id, role=user.role)
    task = TaskCreate(intent=record.intent, title=record.intent[:60])
    handle = await run_task(task, ctx)
    logger.info("manual replan | from=%s to=%s reason=%s", run_id, handle.run_id, request.reason)
    return RunHandleResponse(**handle.to_dict())
