"""Authenticated, tenant-scoped human workflow decisions."""

import logging
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from forgeflow.api.dependencies import get_current_user, get_graph, get_pool
from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.schemas import ApprovalActionRequest, ApprovalRequestResponse
from forgeflow.rbac.models import UserContext
from forgeflow.repositories.approval_requests import ApprovalRequestsRepository
from forgeflow.workflows.sales_ops.pipeline import SalesOpsPipeline

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/pending", response_model=list[ApprovalRequestResponse])
async def list_pending_approvals(
    pool: asyncpg.Pool = Depends(get_pool),
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    return [_row_to_schema(r) for r in await ApprovalRequestsRepository(pool).pending(tenant)]


@router.get("/{token}", response_model=ApprovalRequestResponse)
async def get_approval(
    token: str,
    pool: asyncpg.Pool = Depends(get_pool),
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    return _row_to_schema(await ApprovalRequestsRepository(pool).get(tenant, token))


async def _decide(
    token: str,
    body: ApprovalActionRequest,
    pool: Any,
    graph: Any,
    user: UserContext,
    tenant: str,
    decision: str,
):
    repo = ApprovalRequestsRepository(pool)
    run_id, thread_id = await repo.claim(
        tenant, token, decision, user.user_id, body.reason or body.note
    )
    try:
        state = await SalesOpsPipeline(graph).resume(
            thread_id=thread_id, approval_status=decision, resolved_by=user.user_id
        )
        if state.get("errors"):
            raise RuntimeError("Workflow resumed with execution errors")
        status = (
            "rejected"
            if decision == "rejected"
            else ("completed" if state.get("current_stage") == "done" else "pending_approval")
        )
        await repo.finish(tenant, token, run_id, decision, status)
    except Exception as exc:
        # The persisted claim must NOT be undone: an external effect may have
        # happened. Operators can reconcile resuming_*; clients cannot replay it.
        logger.exception("Approval resume needs reconciliation | token=%s", token)
        raise HTTPException(
            503, "Decision recorded; workflow resume requires reconciliation"
        ) from exc
    return {"status": decision, "thread_id": thread_id}


@router.post("/{token}/approve")
async def approve(
    token: str,
    body: ApprovalActionRequest,
    pool: asyncpg.Pool = Depends(get_pool),
    graph: Any = Depends(get_graph),
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    return await _decide(token, body, pool, graph, user, tenant, "approved")


@router.post("/{token}/reject")
async def reject(
    token: str,
    body: ApprovalActionRequest,
    pool: asyncpg.Pool = Depends(get_pool),
    graph: Any = Depends(get_graph),
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    return await _decide(token, body, pool, graph, user, tenant, "rejected")


def _row_to_schema(row: dict) -> ApprovalRequestResponse:
    return ApprovalRequestResponse(
        id=str(row["id"]),
        run_id=str(row["run_id"]),
        token=str(row["token"]),
        stage=row["stage"],
        status=row["status"],
        payload=row["payload"],
        requested_at=row["requested_at"],
        expires_at=row["expires_at"],
        resolved_at=row.get("resolved_at"),
        resolution_note=row.get("resolution_note"),
    )
