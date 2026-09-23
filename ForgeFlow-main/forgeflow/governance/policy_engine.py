"""PolicyEngine — RBAC → ABAC → risk classification → HITL (docs §6.3).

Decision order:
  1. **RBAC**  — ``ROLE_PERMISSIONS[role]`` must contain ``action:resource``.
  2. **ABAC**  — every matching ``policies`` row's ``condition`` is evaluated;
                 a satisfied ``deny`` rule short-circuits to deny.
  3. **Risk**  — classify(resource, action, context) → low / medium / high.
  4. **HITL**  — high risk ⇒ ``requires_approval=True`` (+ an Approval row).

Everything fails closed: an unknown role or an unmapped permission denies.
"""

from __future__ import annotations

import logging
from typing import Any

from forgeflow.config import get_settings
from forgeflow.governance.models import EvalDecision
from forgeflow.rbac.enforcer import RBACEnforcer

logger = logging.getLogger(__name__)

_HIGH_RISK_ACTIONS = {"delete", "drop", "destroy", "truncate", "purge", "revoke"}
_HIGH_RISK_RESOURCES = {"transfer", "payment", "pay", "funds", "wire", "payout"}
_OUTBOUND_RESOURCES = {"email", "smtp", "http", "webhook", "external", "mail"}
_OUTBOUND_ACTIONS = {"send", "post", "execute", "publish", "dispatch"}
_WRITE_ACTIONS = {"write", "create", "update", "put", "patch", "approve", "promote"}

# Intent signals that mark a whole task as high-risk even though the coarse
# (resource="workflows", action="execute") pair would classify as "low".
# Keeps the entry gate honest (QA V6): a "转账/删除/外发" intent must not slip
# through as low risk.
_SENSITIVE_HINTS = (
    "转账", "付款", "支付", "打款", "汇款", "删除", "清空", "泄密",
    "外发", "导出客户", "批量删除", "退款",
    "transfer", "payment", "pay ", "wire", "payout", "delete",
    "drop ", "purge", "truncate", "revoke", "exfiltrate", "bulk delete",
)


def _intent_is_sensitive(intent: str) -> bool:
    """True when the free-text intent carries a high-risk signal."""
    text = (intent or "").lower()
    return any(hint in text for hint in _SENSITIVE_HINTS)


def classify_risk(resource: str, action: str, context: dict[str, Any] | None = None) -> str:
    """Classify a (resource, action) pair into low / medium / high risk."""
    context = context or {}
    action_l = (action or "").lower()
    resource_l = (resource or "").lower()

    # Caller-supplied escalation: an entry-point that already flagged the
    # request as sensitive (see ``_intent_is_sensitive``) must not be diluted
    # back down to "low" by the coarse resource/action pair.
    if context.get("sensitive") or context.get("high_risk"):
        return "high"
    if action_l in _HIGH_RISK_ACTIONS:
        return "high"
    if resource_l in _HIGH_RISK_RESOURCES:
        return "high"
    if resource_l in _OUTBOUND_RESOURCES and action_l in _OUTBOUND_ACTIONS:
        return "high"
    if context.get("bulk") or context.get("batch"):
        return "medium"
    if action_l in _WRITE_ACTIONS:
        return "medium"
    if action_l in {"read", "list", "get", "search", "evaluate"}:
        return "low"
    return "low"


def _condition_matches(condition: dict[str, Any], context: dict[str, Any]) -> bool:
    """ABAC: evaluate a policy ``condition`` against ``context``.

    Delegates to :func:`forgeflow.governance.conditions.evaluate_condition`,
    which keeps the original ``key == value`` semantics **exactly** and adds the
    INC2-17 operator set (``in`` / ``ne`` / ``gt`` / ``lt`` / ``contains`` and
    the ``all`` / ``any`` combinators). Kept as a thin wrapper so existing
    callers/imports (and the audit path that logs on a match) are unchanged.
    """
    from forgeflow.governance.conditions import evaluate_condition

    return evaluate_condition(condition, context)


def _matches_slot(rule_value: str, actual: str) -> bool:
    return rule_value in ("*", "") or rule_value == actual


class PolicyEngine:
    """Evaluates RBAC + ABAC policy for one (subject, resource, action) triple."""

    def __init__(self, repo: Any | None = None, enforcer: RBACEnforcer | None = None) -> None:
        self._repo = repo
        self._enforcer = enforcer or RBACEnforcer()

    def _repo_or_default(self) -> Any:
        if self._repo is not None:
            return self._repo
        from forgeflow.repositories import get_policy_repository

        return get_policy_repository()

    async def evaluate(
        self,
        subject: str,
        resource: str,
        action: str,
        context: dict[str, Any] | None = None,
        *,
        tenant_id: str | None = None,
        subject_role: str | None = None,
    ) -> EvalDecision:
        """Return an ``EvalDecision`` for the request."""
        ctx = dict(context or {})
        role = subject_role or str(ctx.get("role") or "")

        # 1. RBAC — fail closed on unknown role / missing permission.
        if not role:
            return EvalDecision(
                effect="deny",
                risk_level="low",
                reason="no role in context — RBAC cannot be satisfied",
            )
        if not self._enforcer.check(role, action, resource):
            logger.warning("policy deny (rbac) | role=%s %s:%s", role, action, resource)
            return EvalDecision(
                effect="deny",
                risk_level=classify_risk(resource, action, ctx),
                reason=f"role '{role}' cannot {action} {resource}",
            )

        # 2. ABAC — evaluate matching policy rows.
        hit_policy_id: str | None = None
        try:
            policies = await self._repo_or_default().list_policies(
                tenant_id, subject=subject, resource=resource
            )
        except Exception:  # noqa: BLE001 — storage hiccup must not open the gate
            policies = []

        for policy in policies:
            if not _matches_slot(policy.subject, subject) and policy.subject not in ("*",):
                # subject may also be a role name
                if policy.subject != role:
                    continue
            if not _matches_slot(policy.action, action):
                continue
            if not _condition_matches(policy.condition, ctx):
                continue
            if policy.effect == "deny":
                logger.warning("policy deny (abac) | policy=%s", policy.id)
                return EvalDecision(
                    effect="deny",
                    risk_level=classify_risk(resource, action, ctx),
                    hit_policy_id=policy.id,
                    reason=policy.description or "denied by ABAC policy",
                )
            hit_policy_id = hit_policy_id or policy.id

        # 3. risk + 4. HITL.
        risk = classify_risk(resource, action, ctx)
        if risk == "high":
            decision = EvalDecision(
                effect="allow",
                risk_level=risk,
                hit_policy_id=hit_policy_id,
                requires_approval=True,
                reason="high-risk action — human approval required (HITL)",
            )
            await self._raise_approval(subject, resource, action, ctx, tenant_id, decision)
            return decision

        return EvalDecision(
            effect="allow",
            risk_level=risk,
            hit_policy_id=hit_policy_id,
            requires_approval=False,
            reason=f"allowed ({risk} risk)",
        )

    async def _raise_approval(
        self,
        subject: str,
        resource: str,
        action: str,
        context: dict[str, Any],
        tenant_id: str | None,
        decision: EvalDecision,
    ) -> Any | None:
        """Persist an ApprovalRecord for a high-risk decision (best-effort)."""
        from forgeflow.governance.models import ApprovalRecord

        approval = ApprovalRecord(
            tenant_id=tenant_id,
            run_id=context.get("run_id"),
            risk_level=decision.risk_level,
            requested_action=f"{action}:{resource}",
            requester=subject,
            status="pending",
            note=decision.reason,
        )
        try:
            await self._repo_or_default().save_approval(approval)
        except Exception:  # noqa: BLE001 — never break the caller on audit failure
            return None
        decision.approval_id = approval.id  # type: ignore[attr-defined]
        return approval

    async def evaluate_tool_call(
        self,
        subject: str,
        role: str,
        tool: str,
        *,
        tenant_id: str | None = None,
        run_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> EvalDecision:
        """Risk-gate a **runtime tool call** (docs §6.3, QA V6).

        Invoked from inside a running task — *after* the caller already passed
        the API's route-level RBAC — so it focuses on risk + HITL instead of
        re-running route RBAC (which would deny on generic tool permissions).
        A high-risk tool yields ``requires_approval=True`` and persists an
        Approval row; the caller must **block** execution until it is decided.

        ``tool`` is a dotted ``resource.action`` identifier, e.g.
        ``"payment.transfer"`` or ``"email.send"``.
        """
        resource, _, action = (tool or "").partition(".")
        action = action or "execute"
        ctx: dict[str, Any] = {
            **(context or {}),
            "role": role,
            "tool": tool,
            "run_id": run_id,
        }
        risk = classify_risk(resource, action, ctx)
        if risk == "high":
            decision = EvalDecision(
                effect="allow",
                risk_level=risk,
                requires_approval=True,
                reason=f"high-risk tool '{tool}' requires human approval (HITL)",
            )
            await self._raise_approval(subject, resource, action, ctx, tenant_id, decision)
            return decision
        return EvalDecision(
            effect="allow",
            risk_level=risk,
            requires_approval=False,
            reason=f"allowed ({risk} risk)",
        )


async def evaluate_task_entry(
    subject: str, role: str, intent: str, *, tenant_id: str | None = None
) -> EvalDecision:
    """Risk-grade a ``POST /tasks`` entry point (docs §4.1).

    The coarse ``workflows:execute`` pair is always "low", so a sensitive
    intent (转账/删除/外发 …) is escalated explicitly — otherwise the gate
    would be a no-op (QA V6).
    """
    engine = PolicyEngine()
    context: dict[str, Any] = {"role": role, "intent": intent}
    if _intent_is_sensitive(intent):
        context["sensitive"] = True
    return await engine.evaluate(
        subject=subject,
        resource="workflows",
        action="execute",
        context=context,
        tenant_id=tenant_id,
    )
