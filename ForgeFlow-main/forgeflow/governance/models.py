"""Domain models for the Security Hub (policies, approvals, decisions)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from forgeflow.repositories.base import new_id, utcnow

RISK_LEVELS = ("low", "medium", "high")
POLICY_EFFECTS = ("allow", "deny")


@dataclass
class PolicyRecord:
    """An RBAC/ABAC rule (``policies`` table)."""

    id: str = field(default_factory=new_id)
    tenant_id: str | None = None
    subject: str = "*"          # role / user id / "*"
    resource: str = "*"         # e.g. "skills", "transfer", "*"
    action: str = "*"           # e.g. "write", "execute", "*"
    condition: dict[str, Any] = field(default_factory=dict)
    effect: str = "allow"       # allow | deny
    description: str = ""
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        return data


@dataclass
class ApprovalRecord:
    """A human-in-the-loop approval request (``approval_requests`` + risk)."""

    id: str = field(default_factory=new_id)
    tenant_id: str | None = None
    run_id: str | None = None
    risk_level: str = "low"
    requested_action: str = ""
    requester: str | None = None
    approver: str | None = None
    decision: str | None = None       # approve | reject | None (pending)
    status: str = "pending"           # pending | approved | rejected
    note: str = ""
    # Evolution taxonomy (migration 011 added ``agent_approvals.kind``):
    # "skill.optimize" | "skill.retire" | None for legacy/generic approvals.
    kind: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    resolved_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        data["resolved_at"] = self.resolved_at.isoformat() if self.resolved_at else None
        return data


@dataclass
class EvalDecision:
    """Result of a PolicyEngine evaluation (docs §6.3)."""

    effect: str = "allow"                 # allow | deny
    risk_level: str = "low"
    hit_policy_id: str | None = None
    requires_approval: bool = False
    reason: str = ""
    approval_id: str | None = None

    @property
    def allowed(self) -> bool:
        return self.effect == "allow"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
