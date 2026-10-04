"""INC46 T21 — the four-level → :class:`PendingAction` policy.

This module turns a *privilege level* (the T04 classification
``READ/WRITE/EXTERNAL/DANGEROUS``) plus a tenant's policy into a concrete plan:

* what action to take (auto / pause for approval / confirm / deny);
* the :class:`~forgeflow.hitl.pending.PendingAction` to persist (or ``None``);
* an **audit record** that is *always* produced — mandatory whenever the run is
  allowed through without a human (a low-risk WRITE auto-approval must leave a
  trace, 红线 21).

The classification itself is **not** re-implemented here: :func:`plan_for_tool`
calls :func:`forgeflow.skills.tool_permissions.classify_tool` (T04) and passes the
resulting level in — there is exactly one classifier in the codebase.

Tenant policy
-------------
The only recognised key today is ``{"auto_approve_low_risk": True}``. Unknown
keys are ignored (read as ``False``) so a future policy cannot silently widen the
gate. :func:`read_tenant_policy` is the single reader.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from forgeflow.hitl.pending import (
    ACTION_AUTO,
    ACTION_DENY,
    ACTION_PENDING_APPROVAL,
    DEFAULT_TIMEOUT_HOURS,
    PendingAction,
    decide_permission_level,
    default_expiry,
)
from forgeflow.repositories.base import utcnow
from forgeflow.skills.tool_permissions import classify_tool

logger = logging.getLogger(__name__)

__all__ = [
    "AUTO_APPROVE_LOW_RISK_KEY",
    "read_tenant_policy",
    "decide",
    "pending_kind_for",
    "should_pause",
    "build_pending_action",
    "build_audit_record",
    "plan",
    "plan_for_tool",
]

#: The one tenant-policy key read today.
AUTO_APPROVE_LOW_RISK_KEY = "auto_approve_low_risk"


def read_tenant_policy(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Normalise a tenant's HITL policy to the recognised keys.

    Unknown keys are dropped and everything defaults to ``False`` (a missing or
    malformed policy never widens the gate). Returns
    ``{"auto_approve_low_risk": bool}``.
    """
    policy = raw if isinstance(raw, dict) else {}
    return {AUTO_APPROVE_LOW_RISK_KEY: bool(policy.get(AUTO_APPROVE_LOW_RISK_KEY))}


def decide(
    level: str,
    *,
    tenant_policy: dict[str, Any] | None = None,
    is_admin_policy: bool = False,
) -> dict[str, Any]:
    """Thin wrapper over :func:`decide_permission_level` with policy normalisation."""
    return decide_permission_level(
        level,
        tenant_policy=read_tenant_policy(tenant_policy),
        is_admin_policy=is_admin_policy,
    )


def pending_kind_for(
    level: str,
    *,
    tenant_policy: dict[str, Any] | None = None,
    is_admin_policy: bool = False,
) -> str | None:
    """The :class:`PendingAction` kind this level needs, or ``None`` (auto/deny)."""
    return decide(
        level, tenant_policy=tenant_policy, is_admin_policy=is_admin_policy
    ).get("kind")


def should_pause(
    level: str,
    *,
    tenant_policy: dict[str, Any] | None = None,
    is_admin_policy: bool = False,
) -> bool:
    """Whether this level produces a pending action (i.e. the run pauses)."""
    action = decide(
        level, tenant_policy=tenant_policy, is_admin_policy=is_admin_policy
    )["action"]
    return action in (ACTION_PENDING_APPROVAL, "confirm_permission")


def build_pending_action(
    level: str,
    *,
    run_id: str,
    payload: dict[str, Any] | None = None,
    pending_id: str | None = None,
    tenant_id: str | None = None,
    tenant_policy: dict[str, Any] | None = None,
    is_admin_policy: bool = False,
    timeout_hours: int = DEFAULT_TIMEOUT_HOURS,
    now: datetime | None = None,
) -> PendingAction | None:
    """Build the :class:`PendingAction` to persist for ``level`` (unsaved).

    Returns ``None`` when no human is needed — ``auto`` (READ, or a tenant
    auto-approved WRITE) or ``deny`` (a DANGEROUS tool with no admin policy).
    The caller persists the returned action (via
    :meth:`PendingActionStore.create`) and, for the non-pausing paths, records
    the audit entry from :func:`build_audit_record`.
    """
    decision = decide(level, tenant_policy=tenant_policy, is_admin_policy=is_admin_policy)
    kind = decision.get("kind")
    if not kind or decision["action"] == ACTION_DENY:
        return None

    moment = now or utcnow()
    return PendingAction(
        pending_id=pending_id or f"pa-{run_id}-{decision['kind']}",
        run_id=run_id,
        kind=str(kind),
        payload=dict(payload or {}),
        expires_at=default_expiry(moment, timeout_hours),
        tenant_id=tenant_id,
        created_at=moment,
    )


def build_audit_record(
    level: str,
    run_id: str,
    decision: dict[str, Any],
    *,
    tool: str | None = None,
    actor: str | None = None,
    tenant_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The audit entry for a HITL decision (mandatory for a non-pausing allow).

    Always produced so an auto-approval is never silent. ``decision`` is the dict
    returned by :func:`decide`.
    """
    return {
        "event": "hitl_decision",
        "level": level,
        "action": decision.get("action"),
        "kind": decision.get("kind"),
        "reason": decision.get("reason"),
        "run_id": run_id,
        "tool": tool,
        "actor": actor,
        "tenant_id": tenant_id,
        "at": (now or utcnow()).isoformat(),
    }


def plan(
    level: str,
    *,
    run_id: str,
    payload: dict[str, Any] | None = None,
    pending_id: str | None = None,
    tenant_id: str | None = None,
    tenant_policy: dict[str, Any] | None = None,
    is_admin_policy: bool = False,
    tool: str | None = None,
    actor: str | None = None,
    timeout_hours: int = DEFAULT_TIMEOUT_HOURS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Full plan: the decision, the (optional) pending action and its audit entry.

    Returns ``{"level", "decision", "pending", "audit"}`` where ``pending`` is a
    :class:`PendingAction` (unsaved) or ``None``, and ``audit`` is the
    :func:`build_audit_record` entry. The audit entry is emitted at ``INFO`` so a
    low-risk auto-approval leaves a trace even if the caller does not persist it.
    """
    decision = decide(level, tenant_policy=tenant_policy, is_admin_policy=is_admin_policy)
    pending = build_pending_action(
        level,
        run_id=run_id,
        payload=payload,
        pending_id=pending_id,
        tenant_id=tenant_id,
        tenant_policy=tenant_policy,
        is_admin_policy=is_admin_policy,
        timeout_hours=timeout_hours,
        now=now,
    )
    audit = build_audit_record(
        level, run_id, decision, tool=tool, actor=actor, tenant_id=tenant_id, now=now
    )
    if decision["action"] == ACTION_AUTO:
        logger.info("HITL auto-allow | %s", audit)
    return {"level": level, "decision": decision, "pending": pending, "audit": audit}


def plan_for_tool(
    tool: str,
    *,
    run_id: str,
    payload: dict[str, Any] | None = None,
    pending_id: str | None = None,
    tenant_id: str | None = None,
    tenant_policy: dict[str, Any] | None = None,
    is_admin_policy: bool = False,
    actor: str | None = None,
    timeout_hours: int = DEFAULT_TIMEOUT_HOURS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Classify ``tool`` (T04) and plan the HITL response. Same shape as :func:`plan`."""
    level = classify_tool(tool)
    return plan(
        level,
        run_id=run_id,
        payload=payload,
        pending_id=pending_id,
        tenant_id=tenant_id,
        tenant_policy=tenant_policy,
        is_admin_policy=is_admin_policy,
        tool=tool,
        actor=actor,
        timeout_hours=timeout_hours,
        now=now,
    )
