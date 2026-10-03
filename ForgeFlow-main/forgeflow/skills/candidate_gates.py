"""INC46 T08 — candidate admission gate + reusable cross-skill conflict detection.

Why this module
---------------
T07 gave the contract a shape; the critic (jump ④) emits findings, but a
finding and a *decision* are different things. This module turns the critic's
findings **plus** the derived risk tier (T08
:mod:`forgeflow.skills.risk_escalation`) into one explicit, reproducible
admission decision:

* :func:`evaluate_candidate_gate` → a :class:`CandidateGateResult`
  (``allowed`` / ``risk_level`` / ``blocking_codes`` / ``findings``);
* :func:`blocks_auto_publish` → whether the candidate must be human-gated.

Two separable concerns live here on purpose:

1. **Admission** — a *single* candidate's fitness, composed from the critic and
   the risk tier. The interlock requirement **R1** ("Candidate Gate / Critic
   增强生效，DANGEROUS 候选阻断自动发布") is satisfied here, and exposed to the
   publish interlock through :func:`INTERLOCK_PROBE` (the T15 forward contract).
2. **Conflict detection** — :func:`detect_conflicts` compares **two** skills and
   returns conflict findings. It is a **standalone, critic-independent** function
   (it imports only the contract shape), so T35's cross-Skill conflict detection
   reuses it directly instead of re-implementing the rules.

Fail-closed / honesty
---------------------
* ``DANGEROUS`` tools ⇒ effective risk ``high`` ⇒ auto-publish blocked **and**
  HITL required, even if the contract declares ``risk_level="low"``.
* The two safety invariants ``missing_failure_path`` and ``no_termination`` are
  the critic's *medium* severity — but the gate treats them as **blocking**
  (:data:`GATE_BLOCKING_CODES`): "缺少失败路径 / 无终止条件 ⇒ 不得放行". A mere
  finding is never silently promoted to a pass.
* No mock, no LLM: the gate is deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from forgeflow.skills import risk_escalation as _risk
from forgeflow.skills.contracts import HIGH_RISK, SkillContract
from forgeflow.skills.critic import critique

__all__ = [
    "GATE_BLOCKING_CODES",
    "CONFLICT_CODES",
    "CandidateGateResult",
    "evaluate_candidate_gate",
    "blocks_auto_publish",
    "detect_conflicts",
    "INTERLOCK_PROBE",
]

#: Existing *medium* codes that the gate promotes to **blocking**. These encode
#: the two safety invariants the task book calls out: a candidate with no failure
#: / rollback path, or with no evident termination bound, must not pass the gate
#: (仅记录 finding 而放行是不允许的). Everything else blocks only at ``high``.
GATE_BLOCKING_CODES: frozenset[str] = frozenset(
    {"missing_failure_path", "no_termination"}
)

#: The conflict codes :func:`detect_conflicts` may emit (T35 reuses these).
CONFLICT_CODES: tuple[str, ...] = ("io_type_conflict", "overlapping_trigger")


@dataclass
class CandidateGateResult:
    """The gate's decision over one candidate — explicit and reproducible."""

    allowed: bool = False
    risk_level: str = "low"
    declared_risk: str = "low"
    escalated: bool = False
    dangerous_tools: list[str] = field(default_factory=list)
    blocking_codes: list[str] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def blocks_auto_publish(self) -> bool:
        """Auto-publish is blocked when the candidate is blocked OR high-risk."""
        return (not self.allowed) or self.risk_level == HIGH_RISK

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "risk_level": self.risk_level,
            "declared_risk": self.declared_risk,
            "escalated": self.escalated,
            "dangerous_tools": list(self.dangerous_tools),
            "blocking_codes": list(self.blocking_codes),
            "findings": [dict(f) for f in self.findings],
            "reasons": list(self.reasons),
            "blocks_auto_publish": self.blocks_auto_publish,
        }


def _is_blocking(finding: dict[str, Any]) -> bool:
    """A finding blocks the gate iff ``high`` or a :data:`GATE_BLOCKING_CODES`."""
    if str(finding.get("severity")) == "high":
        return True
    return str(finding.get("code")) in GATE_BLOCKING_CODES


def evaluate_candidate_gate(candidate: Any) -> CandidateGateResult:
    """Compose the critic + derived risk into one admission decision.

    Args:
        candidate: a :class:`SkillContract`, a contract/draft-spec dict, or a T07
            seven-segment document (normalised by
            :func:`forgeflow.skills.risk_escalation.as_contract`).

    Returns:
        A :class:`CandidateGateResult`. ``allowed`` is False when any ``high``
        finding exists or a :data:`GATE_BLOCKING_CODES` finding is present.
        ``risk_level`` is the **effective** tier (DANGEROUS ⇒ ``high``), so even
        an otherwise-clean dangerous candidate still ``blocks_auto_publish``.
    """
    contract: SkillContract = _risk.as_contract(candidate)
    crit = critique(contract)
    esc = _risk.risk_escalation(contract)

    blocking_codes = [str(f["code"]) for f in crit.findings if _is_blocking(f)]
    # De-duplicate while preserving order (a code appears once per rule).
    seen: set[str] = set()
    blocking_codes = [c for c in blocking_codes if not (c in seen or seen.add(c))]

    reasons: list[str] = []
    if blocking_codes:
        reasons.append("闸门阻断项：" + "、".join(blocking_codes))
    reasons.extend(esc.reasons)

    return CandidateGateResult(
        allowed=not blocking_codes,
        risk_level=esc.effective,
        declared_risk=esc.declared,
        escalated=esc.escalated,
        dangerous_tools=list(esc.dangerous_tools),
        blocking_codes=blocking_codes,
        findings=[dict(f) for f in crit.findings],
        reasons=reasons,
    )


def blocks_auto_publish(candidate: Any) -> bool:
    """Whether ``candidate`` must be human-gated (never auto-published)."""
    return evaluate_candidate_gate(candidate).blocks_auto_publish


# --------------------------------------------------------------------------- #
# reusable cross-skill conflict detection (T35 consumes this)                   #
# --------------------------------------------------------------------------- #
def _io_types(contract: SkillContract, field: str) -> dict[str, str]:
    """``{key: type}`` for one io field, normalised to lower-case keys + str type."""
    raw = getattr(contract, field, {}) or {}
    return {str(k).strip().lower(): str(v).strip() for k, v in raw.items()}


def detect_conflicts(left: Any, right: Any) -> list[dict[str, Any]]:
    """Compare two skills and return conflict findings (critic-shape dicts).

    Standalone and critic-independent — import this directly (T35). Each finding
    is ``{"code","severity","message","field"}`` (the same shape the critic uses,
    so a reviewer/UI can render both uniformly).

    Rules (deterministic):

    * ``io_type_conflict`` (``high``): the two skills share an input **or**
      output key whose declared *type* differs. A downstream dispatcher cannot
      satisfy both at once, so it is a hard conflict.
    * ``overlapping_trigger`` (``medium``): both declare a non-empty
      ``applicable_when`` and share at least one identical ``(key, value)``
      pair (same trigger) with differing goals — the two skills would compete
      for the same trigger.

    Args:
        left, right: any value :func:`forgeflow.skills.risk_escalation.as_contract`
            accepts.

    Returns:
        A (possibly empty) list of conflict findings.
    """
    a = _risk.as_contract(left)
    b = _risk.as_contract(right)
    findings: list[dict[str, Any]] = []

    for field in ("inputs", "outputs"):
        ta, tb = _io_types(a, field), _io_types(b, field)
        for key in sorted(set(ta) & set(tb)):
            if ta[key] != tb[key]:
                findings.append(
                    {
                        "code": "io_type_conflict",
                        "severity": "high",
                        "message": (
                            f"{field}['{key}'] 类型冲突：{ta[key]!r} vs {tb[key]!r}"
                        ),
                        "field": f"{field}.{key}",
                    }
                )

    shared_triggers = sorted(
        (k, v)
        for k, v in (a.applicable_when or {}).items()
        if (b.applicable_when or {}).get(k) == v
    )
    if shared_triggers and a.goal != b.goal:
        pairs = ", ".join(f"{k}={v!r}" for k, v in shared_triggers)
        findings.append(
            {
                "code": "overlapping_trigger",
                "severity": "medium",
                "message": f"两个 skill 触发条件重叠（{pairs}）但目标不同，会争用同一触发",
                "field": "applicable_when",
            }
        )

    return findings


# --------------------------------------------------------------------------- #
# interlock forward contract (T15 R1)                                          #
# --------------------------------------------------------------------------- #
def INTERLOCK_PROBE() -> dict[str, Any]:
    """T15 forward-contract probe for requirement **R1**.

    Fail-closed self-check: the probe only reports ``ok`` when the gate actually
    blocks a DANGEROUS candidate **and** escalates it to ``high``. If the gate
    ever regresses (e.g. the ``dangerous_operation`` rule is dropped), the probe
    reports unmet with a concrete reason — the interlock then stays locked
    instead of trusting a hollow module. No LLM, no I/O.
    """
    dangerous = SkillContract(
        goal="转账付款",
        procedure=["读取金额", "发起转账"],
        tools=["payment.transfer"],
        verification=["失败则回滚"],
        risk_level="low",
    )
    result = evaluate_candidate_gate(dangerous)
    if result.risk_level != HIGH_RISK or not result.blocks_auto_publish:
        return {
            "ok": False,
            "evidence": (
                "候选闸门自检失败：DANGEROUS 候选未被升级为 high 或未被阻断自动发布"
                f"（risk_level={result.risk_level}, blocks={result.blocks_auto_publish}）"
            ),
        }
    return {
        "ok": True,
        "evidence": (
            "tests/unit/test_inc46_candidate_gates.py：DANGEROUS 工具 ⇒ 风险升级 high "
            "且 blocks_auto_publish=True（candidate_gates.evaluate_candidate_gate 自检通过）"
        ),
    }
