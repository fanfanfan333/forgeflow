"""Tenant-scoped legacy approvals with a durable, one-shot decision claim."""

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

from forgeflow.database import tenant_connection


class ApprovalRequestsRepository:
    def __init__(self, pool: Any) -> None:
        self.pool = pool

    async def pending(self, tenant: str):
        async with tenant_connection(self.pool, tenant) as conn:
            return await conn.fetch(
                "SELECT * FROM approval_requests WHERE tenant_id=$1 AND status='pending' AND expires_at>now() ORDER BY requested_at DESC",
                tenant,
            )

    async def get(self, tenant: str, token: str):
        try:
            key = uuid.UUID(token)
        except ValueError as exc:
            raise HTTPException(400, "Invalid token format") from exc
        async with tenant_connection(self.pool, tenant) as conn:
            row = await conn.fetchrow(
                "SELECT * FROM approval_requests WHERE token=$1 AND tenant_id=$2", key, tenant
            )
        if row is None:
            raise HTTPException(404, "Approval request not found")
        return dict(row)

    async def claim(self, tenant: str, token: str, decision: str, actor: str, note: str | None):
        row = await self.get(tenant, token)
        if row["status"] != "pending":
            raise HTTPException(409, "Approval already resolved or in progress")
        if row["expires_at"].astimezone(UTC) <= datetime.now(UTC):
            raise HTTPException(410, "Approval request has expired")
        async with tenant_connection(self.pool, tenant) as conn:
            run = await conn.fetchrow(
                "SELECT thread_id FROM workflow_runs WHERE id=$1 AND tenant_id=$2",
                row["run_id"],
                tenant,
            )
            if run is None:
                raise HTTPException(404, "Workflow run not found")
            claimed = await conn.fetchrow(
                """UPDATE approval_requests SET status=$3,
                resolved_by=$4,resolution_note=$5,decision_claimed_at=now()
                WHERE token=$1 AND tenant_id=$2 AND status='pending' AND expires_at>now()
                RETURNING id""",
                uuid.UUID(token),
                tenant,
                "resuming_" + decision,
                actor,
                note,
            )
            if claimed is None:
                raise HTTPException(409, "Approval already resolved or in progress")
        return row["run_id"], str(run["thread_id"])

    async def finish(
        self, tenant: str, token: str, run_id: uuid.UUID, decision: str, workflow_status: str
    ):
        async with tenant_connection(self.pool, tenant) as conn:
            await conn.execute(
                "UPDATE approval_requests SET status=$3,resolved_at=now() WHERE token=$1 AND tenant_id=$2 AND status=$4",
                uuid.UUID(token),
                tenant,
                decision,
                "resuming_" + decision,
            )
            await conn.execute(
                "UPDATE workflow_runs SET status=$3::text,completed_at=CASE WHEN $3::text IN ('completed','rejected') THEN now() ELSE NULL END WHERE id=$1 AND tenant_id=$2",
                run_id,
                tenant,
                workflow_status,
            )
