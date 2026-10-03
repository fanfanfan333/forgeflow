"""BE-3 jump ④ — independent, deterministic critique of a ``SkillContract``.

The critic is the *adversarial reviewer* between DRAFT and TESTING. It is
**offline and deterministic** (no LLM call on the default path): a fixed rule
set inspects the contract and emits findings with an explicit
``{code, severity, message, field}`` shape. This keeps the loop reproducible —
the same contract always yields the same critique, so a repair round can be
proven to fix a named defect.

Fail-closed semantics:

* A ``high`` finding ⇒ its ``code`` is added to ``must_fix``, which **blocks**
  ``CANDIDATE → TESTING`` until a repair round clears it.
* The tool whitelist is the platform catalogue
  (``trust_baseline.allowed_tool_set`` — the single source of truth). A tool
  outside it is a ``high`` finding: the contract could never be dispatched, and
  promoting it would 403 later at the trust baseline anyway — catching it here
  gives a fixable, named defect instead of an opaque late failure.

The optional LLM refinement path is intentionally **not** wired on the default
(``mock``) provider; when no provider is configured the deterministic rules are
the whole critique.
"""

from __future__ import annotations

from typing import Any

from forgeflow.skills import tool_permissions
from forgeflow.skills.contracts import (
    HIGH_RISK,
    RISK_LEVELS,
    SkillContract,
    SkillCritique,
)
from forgeflow.skills.trust_baseline import allowed_tool_set

__all__ = ["critique", "SEVERITY_ORDER"]

#: Ordering used to compute the aggregate severity (higher wins).
SEVERITY_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}

#: INC46 T04 — procedure length beyond which the contract shows no evident
#: termination bound (``no_termination``).
MAX_PROCEDURE_STEPS = 16

#: Verification tokens that evidence an explicit failure / rollback path
#: (``missing_failure_path``).
_FAILURE_PATH_TOKENS = (
    "失败",
    "回滚",
    "rollback",
    "fallback",
    "降级",
    "重试",
    "retry",
    "error",
    "异常",
    "报错",
)

#: Classes that make a contract side-effecting.
_SIDE_EFFECT_CLASSES = (tool_permissions.WRITE, tool_permissions.DANGEROUS)


def _finding(code: str, severity: str, message: str, field_name: str) -> dict[str, Any]:
    """Build one uniform finding dict."""
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "field": field_name,
    }


def _max_severity(findings: list[dict[str, Any]]) -> str:
    """Aggregate severity = the highest severity among ``findings``."""
    if not findings:
        return "none"
    return max(
        (str(f.get("severity", "none")) for f in findings),
        key=lambda sev: SEVERITY_ORDER.get(sev, 0),
    )


def _verification_text(contract: SkillContract) -> str:
    """All declared verification assertions joined into one searchable string."""
    return " ".join(str(v) for v in (contract.verification or []))


def _privilege_findings(contract: SkillContract) -> list[dict[str, Any]]:
    """INC46 T04 — additive privilege / robustness findings.

    Every rule **derives** its judgement from the tool table via
    :mod:`forgeflow.skills.tool_permissions` (no second privilege list). The
    codes emitted here are new; no pre-existing ``critique`` code or severity is
    affected. Conditions are deliberately narrow so a plain, verified,
    read-only contract stays clean:

    * ``dangerous_operation`` (``high``): a ``DANGEROUS`` tool is declared but
      the contract is missing HITL (``risk_level != high``) or a verification
      assertion — money movement / egress / privilege change / ``code.commit``
      must be both human-gated and verified.
    * ``missing_failure_path`` (``medium``): a ``WRITE`` / ``DANGEROUS`` tool is
      declared yet no verification assertion mentions a failure / rollback / retry
      path.
    * ``insufficient_boundary`` (``medium``): a ``WRITE`` / ``EXTERNAL`` /
      ``DANGEROUS`` tool is declared but the contract declares no verification
      boundary assertion at all.
    * ``no_termination`` (``medium``): the ``procedure`` exceeds
      :data:`MAX_PROCEDURE_STEPS` steps — a runaway procedure with no evident
      termination bound.

    A ``high`` finding (``dangerous_operation``) flows into ``must_fix`` through
    the existing mechanism, exactly like every other ``high`` code.
    """
    findings: list[dict[str, Any]] = []
    classes = {t: tool_permissions.classify_tool(t) for t in (contract.tools or [])}
    values = set(classes.values())
    verification_present = bool(contract.verification)

    if tool_permissions.DANGEROUS in values and (
        not verification_present or contract.risk_level != HIGH_RISK
    ):
        dangerous = sorted(t for t, c in classes.items() if c == tool_permissions.DANGEROUS)
        findings.append(
            _finding(
                "dangerous_operation",
                "high",
                f"危险工具 {', '.join(dangerous)} 需 HITL（risk_level=high）且声明校验断言",
                "tools",
            )
        )

    if values & set(_SIDE_EFFECT_CLASSES) and not any(
        token in _verification_text(contract) for token in _FAILURE_PATH_TOKENS
    ):
        findings.append(
            _finding(
                "missing_failure_path",
                "medium",
                "含写 / 危险工具的契约未声明失败 / 回滚路径",
                "verification",
            )
        )

    if values & {
        tool_permissions.WRITE,
        tool_permissions.EXTERNAL,
        tool_permissions.DANGEROUS,
    } and not verification_present:
        findings.append(
            _finding(
                "insufficient_boundary",
                "medium",
                "含写 / 外发 / 危险工具的契约未声明边界校验断言",
                "verification",
            )
        )

    if len(contract.procedure or []) > MAX_PROCEDURE_STEPS:
        findings.append(
            _finding(
                "no_termination",
                "medium",
                f"procedure 步骤数 {len(contract.procedure)} 超过终止上界 "
                f"{MAX_PROCEDURE_STEPS}，疑似无终止条件",
                "procedure",
            )
        )

    return findings


def critique(contract: SkillContract) -> SkillCritique:
    """Run the deterministic rule set and return a :class:`SkillCritique`.

    Args:
        contract: the contract produced by ``synthesize`` (or a repaired copy).

    Returns:
        A critique whose ``findings`` name every structural defect, whose
        ``severity`` is the maximum finding severity, and whose ``must_fix``
        lists the ``code`` of every ``high`` finding (the blockers).
    """
    findings: list[dict[str, Any]] = []

    # --- structural completeness (the four gating elements) --------------
    if not contract.goal:
        findings.append(
            _finding("goal_missing", "high", "契约缺少目标（goal）", "goal")
        )
    if not contract.procedure:
        findings.append(
            _finding("procedure_missing", "high", "契约缺少步骤（procedure）", "procedure")
        )
    if not contract.tools:
        findings.append(
            _finding("tools_missing", "high", "契约未声明任何工具（tools）", "tools")
        )
    else:
        catalogue = allowed_tool_set()
        illegal = sorted({t for t in contract.tools if t not in catalogue})
        if illegal:
            findings.append(
                _finding(
                    "tools_not_whitelisted",
                    "high",
                    f"工具越权：{', '.join(illegal)} 不在平台白名单内",
                    "tools",
                )
            )

    # --- io / verification / policy 覆盖率（非阻断） --------------------
    if not contract.inputs:
        findings.append(_finding("inputs_missing", "medium", "契约未声明输入", "inputs"))
    if not contract.outputs:
        findings.append(
            _finding("outputs_missing", "medium", "契约未声明输出", "outputs")
        )
    if not contract.verification:
        findings.append(
            _finding("verification_missing", "medium", "契约未声明校验断言", "verification")
        )
    if not contract.policies:
        findings.append(
            _finding("policies_missing", "low", "契约未引用任何策略（可接受）", "policies")
        )

    # --- risk tier --------------------------------------------------------
    if contract.risk_level not in RISK_LEVELS:
        findings.append(
            _finding(
                "risk_invalid",
                "high",
                f"风险等级非法：{contract.risk_level!r}",
                "risk_level",
            )
        )
    elif contract.risk_level == HIGH_RISK and not contract.verification:
        findings.append(
            _finding(
                "high_risk_unverified",
                "high",
                "高风险契约必须声明校验断言，否则不得进入 REVIEW",
                "verification",
            )
        )

    # --- INC46 T04 — additive privilege / robustness rules ----------------
    # The four codes below are NEW. Every rule above (and its severity) is
    # untouched; ``dangerous_operation`` (high) joins ``must_fix`` through the
    # same existing mechanism.
    findings.extend(_privilege_findings(contract))

    severity = _max_severity(findings)
    must_fix = [
        str(f["code"]) for f in findings if str(f.get("severity")) == "high"
    ]
    return SkillCritique(findings=findings, severity=severity, must_fix=must_fix)
