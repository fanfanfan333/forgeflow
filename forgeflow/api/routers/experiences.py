"""Experience routes — list, create, and lineage tracing (docs §4.1)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.hub_schemas import (
    ExperienceCreateRequest,
    ExperienceListResponse,
    ExperienceResponse,
    LineageResponse,
)
from forgeflow.experience.embedding import embed_text
from forgeflow.experience.models import ExperienceRecord
from forgeflow.experience.scopes import is_valid_scope  # noqa: F401 — documented import surface
from forgeflow.repositories import get_experience_repository
from forgeflow.runtime.orchestrator import get_run_store

logger = logging.getLogger(__name__)
router = APIRouter()


def _to_response(record: ExperienceRecord) -> ExperienceResponse:
    return ExperienceResponse(
        id=record.id,
        tenant_id=record.tenant_id,
        team_id=record.team_id,
        run_id=record.run_id,
        summary=record.summary,
        decisions=record.decisions,
        outcome=record.outcome,
        reusable_steps=record.reusable_steps,
        tags=record.tags,
        memory_ids=record.memory_ids,
        created_at=record.created_at,
    )


@router.get("", response_model=ExperienceListResponse)
async def list_experiences(
    outcome: str | None = Query(None),
    tag: str | None = Query(None),
    run_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    tenant: str = Depends(resolve_tenant),
):
    """List experiences (newest first), tenant-scoped."""
    repo = get_experience_repository()
    rows = await repo.list(
        tenant, outcome=outcome, tag=tag, run_id=run_id, limit=limit, offset=offset
    )
    total = await repo.count(tenant)
    return ExperienceListResponse(total=total, items=[_to_response(r) for r in rows])


@router.post("", response_model=ExperienceResponse)
async def create_experience(
    request: ExperienceCreateRequest,
    tenant: str = Depends(resolve_tenant),
):
    """Explicitly record an experience (alternative to auto-extraction)."""
    record = ExperienceRecord(
        tenant_id=tenant,
        team_id=request.team_id,
        run_id=request.run_id,
        summary=request.summary or f"任务 {request.run_id} 经验总结",
        decisions=request.decisions,
        outcome=request.outcome,
        reusable_steps=request.reusable_steps,
        tags=request.tags,
        embedding=embed_text(f"{request.summary} {' '.join(request.tags)}"),
        memory_ids=list(request.memory_ids),
    )
    repo = get_experience_repository()
    await repo.save(record)
    for memory_id in request.memory_ids:
        await repo.link_memory(tenant, record.id, memory_id)
    return _to_response(record)


@router.get("/{experience_id}/lineage", response_model=LineageResponse)
async def experience_lineage(experience_id: str, tenant: str = Depends(resolve_tenant)):
    """Trace an experience back to its source Run and Memories."""
    repo = get_experience_repository()
    record = await repo.get(tenant, experience_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Experience not found")

    run = get_run_store().get(record.run_id)
    run_payload = None
    if run is not None:
        run_payload = {
            "run_id": run.run_id,
            "thread_id": run.thread_id,
            "status": run.status,
            "outcome": run.outcome,
            "intent": run.intent,
            "steps": run.steps,
        }
    memories = await repo.list_memories(tenant, experience_id)
    return LineageResponse(
        experience=_to_response(record), run=run_payload, memories=memories
    )
