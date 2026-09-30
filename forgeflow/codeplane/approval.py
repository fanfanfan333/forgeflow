"""Code-task approval state machine (INC25 W2, design §3.2 #25 / §11 U5).

The code-execution plane needs an **execution-time, post-hoc** human gate: the
agent runs a task in an isolated workspace, produces a diff, and then *waits* —
the change is committed only after a person approves it (design §5.2). That is a
different shape from the platform's pre-existing ``PolicyEngine`` HITL (which
fires *before* a tool and hard-stops the run with no resume).

This module is the **single** place that owns that gate. It reuses:

  * ``forgeflow.governance.models.ApprovalRecord`` — the existing HITL record, so
    the decision shows up on ``/approvals`` exactly like any other request
    (``ApprovalRecord.run_id`` links it back to the run), and
  * ``forgeflow.middleware.audit.write_audit_entry`` — the existing nine-field
    audit channel, so every transition is searchable on ``/audit/search``.

Three transitions, one function each:

  * :func:`request_code_approval` — the run reached ``awaiting_approval``: a
    ``pending`` ``ApprovalRecord`` is raised (``kind="code.commit"``) and
    ``codeplane.approval.requested`` is audited;
  * :func:`decide_code_approval` — a person approves / rejects:
    ``codeplane.approval.approved`` / ``codeplane.approval.rejected`` is audited;
  * :func:`find_code_approval` — the read side the decision endpoints use.

The third UI action ("重新分析") is deliberately **not** here: it reuses the
existing ``POST /runs/{id}/replan`` route (design §3.3 #38).
"""

from __future__ import annotations

import logging
from typing import Any

from forgeflow.governance.models import ApprovalRecord
from forgeflow.repositories.base import utcnow

logger = logging.getLogger(__name__)

__all__ = [
    "CODE_APPROVAL_KIND",
    "ACTION_REQUESTED",
    "ACTION_APPROVED",
    "ACTION_REJECTED",
    "request_code_approval",
    "find_code_approval",
    "decide_code_approval",
    "audit_reanalysis",
]

#: The ``ApprovalRecord.kind`` that marks a code-plane commit gate (distinct from
#: the evolution taxonomy values like ``skill.optimize``).
CODE_APPROVAL_KIND = "code.commit"

ACTION_REQUESTED = "codeplane.approval.requested"
ACTION_APPROVED = "codeplane.approval.approved"
ACTION_REJECTED = "codeplane.approval.rejected"
#: The reanalysis action (``POST /runs/{id}/replan``) reuses the existing route;
#: its codeplane-specific audit action lives here so the three actions are one
#: vocabulary (AC-19).
ACTION_REANALYZED = "codeplane.approval.reanalyzed"

#: How many approvals a single run-scoped lookup scans (the repository Protocol
#: has no ``run_id`` filter, so the lookup filters client-side).
_LOOKUP_LIMIT = 200


async def _audit(
    action: str,
    *,
    tenant_id: str | None,
    run_id: str,
    requester: str | None,
    role: str | None,
    outcome: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Best-effort audit write on the existing sink (never raises)."""
    from forgeflow.middleware.audit import write_audit_entry

    try:
        await write_audit_entry(
            {
                "user_id": requester,
                "role": role or "unknown",
                "action": action,
                "resource": "codeplane",
                "resource_id": run_id,
                "outcome": outcome,
                "workspace_id": tenant_id,
                "metadata": dict(metadata or {}),
            }
        )
    except Exception as exc:  # noqa: BLE001 — audit must never break the operation
        logger.warning("code-plane audit write skipped (%s): %s", action, exc)


async def request_code_approval(
    policy_repo: Any,
    *,
    tenant_id: str | None,
    run_id: str,
    requester: str | None,
    note: str = "",
    risk_level: str = "high",
    role: str | None = None,
) -> ApprovalRecord:
    """Raise a ``pending`` code-commit approval for ``run_id`` and audit it.

    Returns the persisted :class:`ApprovalRecord`. A persistence failure is
    logged and the in-memory record is returned anyway — raising it must never
    break the run that just reached ``awaiting_approval``.
    """
    approval = ApprovalRecord(
        tenant_id=tenant_id,
        run_id=run_id,
        risk_level=risk_level,
        requested_action=f"code.commit: {(note or '').strip()[:120]}",
        requester=requester,
        status="pending",
        note=note or "",
        kind=CODE_APPROVAL_KIND,
    )
    saved: ApprovalRecord = approval
    try:
        saved = await policy_repo.save_approval(approval)
    except Exception as exc:  # noqa: BLE001 — HITL persistence must never break a run
        logger.warning("code-plane approval persist failed: %s", exc)
    await _audit(
        ACTION_REQUESTED,
        tenant_id=tenant_id,
        run_id=run_id,
        requester=requester,
        role=role,
        outcome="pending",
        metadata={"approval_id": saved.id, "risk_level": risk_level},
    )
    return saved


async def find_code_approval(
    policy_repo: Any, tenant_id: str | None, run_id: str
) -> ApprovalRecord | None:
    """The code-commit approval for ``run_id`` (a pending one wins), or ``None``."""
    try:
        rows = await policy_repo.list_approvals(tenant_id, limit=_LOOKUP_LIMIT)
    except Exception as exc:  # noqa: BLE001 — a lookup failure is a 404, not a 500
        logger.warning("code-plane approval lookup failed: %s", exc)
        return None
    fallback: ApprovalRecord | None = None
    for record in rows:
        if getattr(record, "run_id", None) != run_id:
            continue
        if getattr(record, "kind", None) != CODE_APPROVAL_KIND:
            continue
        if getattr(record, "status", None) == "pending":
            return record
        fallback = record
    return fallback


async def decide_code_approval(
    policy_repo: Any,
    *,
    tenant_id: str | None,
    run_id: str,
    decision: str,
    approver: str | None,
    note: str = "",
    role: str | None = None,
) -> ApprovalRecord | None:
    """Resolve the run's pending code-commit approval and audit the decision.

    Returns the resolved record, the already-resolved record (idempotent), or
    ``None`` when no code-commit approval exists for the run. ``decision`` is
    ``"approve"`` or ``"reject"`` (anything else is treated as a reject).
    """
    record = await find_code_approval(policy_repo, tenant_id, run_id)
    if record is None:
        return None
    if record.status != "pending":
        return record
    approved = decision == "approve"
    record.decision = "approve" if approved else "reject"
    record.status = "approved" if approved else "rejected"
    record.approver = approver
    if note:
        record.note = note
    record.resolved_at = utcnow()
    try:
        await policy_repo.save_approval(record)
    except Exception as exc:  # noqa: BLE001 — HITL persistence must never break a run
        logger.warning("code-plane approval persist failed: %s", exc)
    await _audit(
        ACTION_APPROVED if approved else ACTION_REJECTED,
        tenant_id=tenant_id,
        run_id=run_id,
        requester=approver,
        role=role,
        outcome="allowed" if approved else "denied",
        metadata={"approval_id": record.id, "decision": record.decision},
    )
    return record


async def audit_reanalysis(
    *,
    tenant_id: str | None,
    run_id: str,
    requester: str | None,
    role: str | None = None,
    from_run_id: str | None = None,
    reason: str = "",
) -> None:
    """Audit the third code-plane action (重新分析) — reuses ``POST /runs/{id}/replan``.

    Public wrapper over :func:`_audit` so the replan route can record the action
    without reaching into a private helper; the three code-plane approval actions
    then share one audit vocabulary (AC-19).
    """
    await _audit(
        ACTION_REANALYZED,
        tenant_id=tenant_id,
        run_id=run_id,
        requester=requester,
        role=role,
        outcome="allowed",
        metadata={"from_run_id": from_run_id, "reason": reason},
    )
