"""PostgreSQL Policy + Approval repository (asyncpg, lazy pool).

HITL records live in a dedicated ``agent_approvals`` table rather than the
legacy ``approval_requests`` one: the existing approvals router/tests depend on
``approval_requests``' exact shape (token/stage/payload), so the hubs keep their
own table to stay schema-compatible (docs C3 / §11 R3).
"""

from __future__ import annotations

import logging
from typing import Any

from forgeflow.governance.models import ApprovalRecord, PolicyRecord
from forgeflow.repositories.base import (
    TenantScopedRepository,
    utcnow,
)

logger = logging.getLogger(__name__)


class PgPolicyRepository(TenantScopedRepository):
    """asyncpg-backed ``PolicyRepository``."""

    def __init__(self, default_tenant: str = "default", pool: Any | None = None) -> None:
        super().__init__(default_tenant)
        self._pool = pool

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        from forgeflow.database import get_pool

        return await get_pool()

    @staticmethod
    def _to_policy(row: Any) -> PolicyRecord:
        d = dict(row)
        return PolicyRecord(
            id=str(d["id"]),
            tenant_id=str(d["tenant_id"]) if d.get("tenant_id") else None,
            subject=d.get("subject") or "*",
            resource=d.get("resource") or "*",
            action=d.get("action") or "*",
            condition=dict(d.get("condition") or {}),
            effect=d.get("effect") or "allow",
            description=d.get("description") or "",
            created_at=d.get("created_at") or utcnow(),
        )

    @staticmethod
    def _to_approval(row: Any) -> ApprovalRecord:
        d = dict(row)
        return ApprovalRecord(
            id=str(d["id"]),
            tenant_id=str(d["tenant_id"]) if d.get("tenant_id") else None,
            run_id=str(d["run_id"]) if d.get("run_id") else None,
            risk_level=d.get("risk_level") or "low",
            requested_action=d.get("requested_action") or "",
            requester=d.get("requester"),
            approver=d.get("approver"),
            decision=d.get("decision"),
            status=d.get("status") or "pending",
            note=d.get("note") or "",
            kind=d.get("kind"),
            created_at=d.get("created_at") or utcnow(),
            resolved_at=d.get("resolved_at"),
        )

    async def save_policy(self, policy: PolicyRecord) -> PolicyRecord:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO policies
                  (id, tenant_id, subject, resource, action, condition, effect,
                   description, created_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                ON CONFLICT (id) DO UPDATE SET
                  subject=EXCLUDED.subject, resource=EXCLUDED.resource,
                  action=EXCLUDED.action, condition=EXCLUDED.condition,
                  effect=EXCLUDED.effect, description=EXCLUDED.description
                """,
                policy.id,
                self.scope_key(policy.tenant_id),
                policy.subject,
                policy.resource,
                policy.action,
                policy.condition,
                policy.effect,
                policy.description,
                policy.created_at,
            )
        return policy

    async def list_policies(
        self,
        tenant_id: str | None,
        *,
        subject: str | None = None,
        resource: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[PolicyRecord]:
        pool = await self._get_pool()
        clauses = ["tenant_id IS NOT DISTINCT FROM $1"]
        args: list[Any] = [self.scope_key(tenant_id)]
        if subject:
            args.append(subject)
            clauses.append(f"subject IN ('*', ${len(args)})")
        if resource:
            args.append(resource)
            clauses.append(f"resource IN ('*', ${len(args)})")
        args += [limit, offset]
        sql = (
            "SELECT * FROM policies WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY created_at DESC LIMIT ${len(args) - 1} OFFSET ${len(args)}"
        )
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [self._to_policy(r) for r in rows]

    async def get_policy(self, tenant_id: str | None, policy_id: str) -> PolicyRecord | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM policies WHERE id=$1 AND tenant_id IS NOT DISTINCT FROM $2",
                policy_id,
                self.scope_key(tenant_id),
            )
        return self._to_policy(row) if row else None

    async def save_approval(self, approval: ApprovalRecord) -> ApprovalRecord:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agent_approvals
                  (id, tenant_id, run_id, risk_level, requested_action, requester,
                   approver, decision, status, note, created_at, resolved_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                ON CONFLICT (id) DO UPDATE SET
                  approver=EXCLUDED.approver, decision=EXCLUDED.decision,
                  status=EXCLUDED.status, note=EXCLUDED.note,
                  resolved_at=EXCLUDED.resolved_at
                """,
                approval.id,
                self.scope_key(approval.tenant_id),
                # ``agent_approvals.run_id`` is opaque TEXT (014) — stored
                # verbatim, so a hub run id is never silently dropped to NULL.
                approval.run_id,
                approval.risk_level,
                approval.requested_action,
                approval.requester,
                approval.approver,
                approval.decision,
                approval.status,
                approval.note,
                approval.created_at,
                approval.resolved_at,
            )
            # ``kind`` is additive (migration 011). Written as a follow-up
            # UPDATE so a database still at revision 010 keeps working and an
            # approval row can never be lost because of a missing column.
            if getattr(approval, "kind", None):
                try:
                    await conn.execute(
                        "UPDATE agent_approvals SET kind=$2 WHERE id=$1",
                        approval.id,
                        approval.kind,
                    )
                except Exception as exc:  # noqa: BLE001 — column absent pre-011
                    logger.debug("agent_approvals.kind write skipped: %s", exc)
        return approval

    async def get_approval(
        self, tenant_id: str | None, approval_id: str
    ) -> ApprovalRecord | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM agent_approvals WHERE id=$1 AND tenant_id IS NOT DISTINCT FROM $2",
                approval_id,
                self.scope_key(tenant_id),
            )
        return self._to_approval(row) if row else None

    async def list_approvals(
        self,
        tenant_id: str | None,
        *,
        status: str | None = None,
        risk_level: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ApprovalRecord]:
        pool = await self._get_pool()
        clauses = ["tenant_id IS NOT DISTINCT FROM $1"]
        args: list[Any] = [self.scope_key(tenant_id)]
        if status:
            args.append(status)
            clauses.append(f"status = ${len(args)}")
        if risk_level:
            args.append(risk_level)
            clauses.append(f"risk_level = ${len(args)}")
        args += [limit, offset]
        sql = (
            "SELECT * FROM agent_approvals WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY created_at DESC LIMIT ${len(args) - 1} OFFSET ${len(args)}"
        )
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [self._to_approval(r) for r in rows]
