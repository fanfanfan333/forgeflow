"""INC46 T08 — derived **risk escalation** for a skill candidate.

Why this module
---------------
A candidate's risk tier must never be trusted from the declaration alone: a
skill can declare ``risk_level="low"`` while binding a money-movement /
egress / privilege-change tool. T04 already derived a four-level tool
classification (:mod:`forgeflow.skills.tool_permissions`); this module lifts
that into the contract's **effective** risk tier:

    effective = max(declared, derived_from_tools)

so a DANGEROUS tool forces ``high`` (and therefore HITL + a blocked
auto-publish), regardless of what the contract claims. It is *pure and
deterministic* — no LLM, no I/O — and it is the single place the escalation
rule lives (the gate layer consumes it; nothing re-derives it).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from forgeflow.skills import tool_permissions
from forgeflow.skills.contracts import HIGH_RISK, LOW_RISK, SkillContract

__all__ = [
    "RISK_ORDER",
    "RiskEscalation",
    "as_contract",
    "derived_risk_level",
    "effective_risk_level",
    "risk_escalation",
    "requires_human_signoff",
    "blocks_auto_publish",
    "DANGEROUS_ESCALATION_REASON",
]

#: Ordering for ``max(declared, derived)``.
RISK_ORDER: dict[str, int] = {LOW_RISK: 0, "medium": 1, HIGH_RISK: 2}

#: Human-readable rationale attached whenever a DANGEROUS tool forces ``high``.
DANGEROUS_ESCALATION_REASON = (
    "声明了 DANGEROUS 工具（收钱 / 外发 / 改权限 / 落库提交）：风险升级为 high，"
    "必须人工签署（HITL）且阻断自动发布"
)


@dataclass
class RiskEscalation:
    """The declared vs. effective risk of one candidate."""

    declared: str = LOW_RISK
    effective: str = LOW_RISK
    dangerous_tools: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def escalated(self) -> bool:
        """True when the effective tier is strictly higher than declared."""
        return RISK_ORDER.get(self.effective, 0) > RISK_ORDER.get(self.declared, 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "declared": self.declared,
            "effective": self.effective,
            "escalated": self.escalated,
            "dangerous_tools": list(self.dangerous_tools),
            "reasons": list(self.reasons),
        }


def as_contract(candidate: Any) -> SkillContract:
    """Normalise ``candidate`` to a :class:`SkillContract` (never raises).

    Accepts a ``SkillContract``, a contract-shaped dict (``goal``/``procedure``),
    a compiler ``draft_spec`` dict (``prompt``/``steps``), or a T07 seven-segment
    document (``manifest``/``procedure``/``tool_bindings``). Anything else ⇒ an
    empty contract (the gate then fails closed on the missing required pieces).
    """
    if isinstance(candidate, SkillContract):
        return candidate
    if isinstance(candidate, dict):
        if "goal" in candidate or "procedure" in candidate:
            return SkillContract.from_dict(candidate)
        if "prompt" in candidate or "steps" in candidate:
            return SkillContract.from_draft_spec(candidate)
        if "manifest" in candidate or "tool_bindings" in candidate:
            return _from_seven_segment(candidate)
    return SkillContract()


def _from_seven_segment(doc: dict[str, Any]) -> SkillContract:
    """Best-effort lift of a T07 seven-segment document into a contract."""
    manifest = doc.get("manifest") or {}
    procedure = doc.get("procedure") or {}
    bindings = doc.get("tool_bindings") or {}
    policies = doc.get("policies") or {}

    def _strings(value: Any, key: str) -> list[str]:
        if isinstance(value, dict):
            return [str(x) for x in (value.get(key) or []) if str(x).strip()]
        if isinstance(value, (list, tuple)):
            return [str(x) for x in value if str(x).strip()]
        return []

    goal = ""
    if isinstance(manifest, dict):
        goal = str(manifest.get("description") or manifest.get("display_name") or "")
    return SkillContract(
        goal=goal,
        procedure=_strings(procedure, "steps"),
        tools=_strings(bindings, "tools"),
        policies=_strings(policies, "constraints"),
    )


def _classes(contract: SkillContract) -> dict[str, str]:
    """``{tool: class}`` for every declared tool (deduped, order-preserving)."""
    out: dict[str, str] = {}
    for tool in contract.tools or []:
        name = str(tool or "").strip()
        if name and name not in out:
            out[name] = tool_permissions.classify_tool(name)
    return out


def derived_risk_level(contract: SkillContract) -> str:
    """The risk tier implied by the declared tools (via T04's classification)."""
    return tool_permissions.risk_level_from_classes(_classes(contract))


def risk_escalation(candidate: Any) -> RiskEscalation:
    """Compute the declared vs. effective risk of ``candidate``."""
    contract = as_contract(candidate)
    declared = str(contract.risk_level or LOW_RISK)
    if declared not in RISK_ORDER:
        declared = LOW_RISK
    classes = _classes(contract)
    derived = tool_permissions.risk_level_from_classes(classes)
    dangerous = sorted(t for t, c in classes.items() if c == tool_permissions.DANGEROUS)

    effective = declared
    if RISK_ORDER.get(derived, 0) > RISK_ORDER.get(effective, 0):
        effective = derived

    reasons: list[str] = []
    if dangerous:
        reasons.append(DANGEROUS_ESCALATION_REASON)
    return RiskEscalation(
        declared=declared, effective=effective, dangerous_tools=dangerous, reasons=reasons
    )


def effective_risk_level(candidate: Any) -> str:
    """Convenience: the effective risk tier (``low`` / ``medium`` / ``high``)."""
    return risk_escalation(candidate).effective


def requires_human_signoff(candidate: Any) -> bool:
    """True when the effective tier is ``high`` (HITL required)."""
    return effective_risk_level(candidate) == HIGH_RISK


def blocks_auto_publish(candidate: Any) -> bool:
    """True when the candidate must **not** be auto-published.

    A DANGEROUS tool always forces ``high`` (see :func:`risk_escalation`), so any
    dangerous candidate is blocked from auto-publish — the concrete enforcement
    behind the interlock requirement R1 ("DANGEROUS 候选阻断自动发布").
    """
    return requires_human_signoff(candidate)
