"""In-memory Policy + Approval repository."""

from __future__ import annotations

import asyncio
from typing import Any

from forgeflow.governance.models import ApprovalRecord, PolicyRecord
from forgeflow.repositories.base import TenantScopedRepository

_POLICIES: dict[str, dict[str, PolicyRecord]] = {}
_APPROVALS: dict[str, dict[str, ApprovalRecord]] = {}
_LOCK = asyncio.Lock()


def clear_policy_store() -> None:
    """Reset all in-memory policy state. Test helper only."""
    _POLICIES.clear()
    _APPROVALS.clear()


class MemoryPolicyRepository(TenantScopedRepository):
    """Dict-backed ``PolicyRepository``."""

    async def save_policy(self, policy: PolicyRecord) -> PolicyRecord:
        key = self.scope_key(policy.tenant_id)
        async with _LOCK:
            _POLICIES.setdefault(key, {})[policy.id] = policy
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
        rows = list(_POLICIES.get(self.scope_key(tenant_id), {}).values())
        if subject:
            rows = [r for r in rows if r.subject in (subject, "*")]
        if resource:
            rows = [r for r in rows if r.resource in (resource, "*")]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[offset : offset + limit]

    async def get_policy(self, tenant_id: str | None, policy_id: str) -> PolicyRecord | None:
        return _POLICIES.get(self.scope_key(tenant_id), {}).get(policy_id)

    async def save_approval(self, approval: ApprovalRecord) -> ApprovalRecord:
        key = self.scope_key(approval.tenant_id)
        async with _LOCK:
            _APPROVALS.setdefault(key, {})[approval.id] = approval
        return approval

    async def get_approval(
        self, tenant_id: str | None, approval_id: str
    ) -> ApprovalRecord | None:
        return _APPROVALS.get(self.scope_key(tenant_id), {}).get(approval_id)

    async def list_approvals(
        self,
        tenant_id: str | None,
        *,
        status: str | None = None,
        risk_level: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ApprovalRecord]:
        rows = list(_APPROVALS.get(self.scope_key(tenant_id), {}).values())
        if status:
            rows = [r for r in rows if r.status == status]
        if risk_level:
            rows = [r for r in rows if r.risk_level == risk_level]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[offset : offset + limit]
