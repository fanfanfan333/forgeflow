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

from forgeflow.skills import segments as _segments
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

#: INC46 T08 — when-to-use cues. The ``description_missing_when_to_use`` heuristic
#: fires only when the goal names *what* to do but gives **no** *when to use it*
#: signal. The judgement is deliberately conservative (误报可控):
#:
#:   * a non-empty ``applicable_when`` **always** counts as "when to use"
#:     (the field exists precisely to declare the trigger);
#:   * otherwise the goal text must contain one of the trigger/scenario cues
#:     below. A plain capability statement like "生成周报" therefore trips the
#:     finding, while "当用户需要生成周报时使用" (或
#:     ``applicable_when={"domain": "报表"}``) is clean.
#:
#: 判据（误报可控）——只收 **多字、语义明确** 的词，避免单字泛匹配：
#:
#:   * 概念词：直接点明「何时 / 场景 / 触发 / 适用」概念（何时、适用、场景…）；
#:   * 触发前导（中文「当…时」家族）：当用户、当需要、需要时、时使用、的情况下…
#:     —— 这些是「何时用」的显式前导，普通能力描述（"生成周报"）不会命中；
#:   * 英文 when-to-use：use when / when the user / applies when…
#:
#: 该 finding 恒为 ``low``（顾问性，不进 ``must_fix``、不阻断闸门），因此偶发漏报
#: 的代价极低；判据宁可保守也不引入会「放过真缺口」的泛词。
_WHEN_TO_USE_TOKENS = (
    # 概念词
    "何时",
    "什么时候",
    "什么情况",
    "适用",  # 适用于…场景
    "场景",
    "触发",
    "时机",
    "用法",
    "用于",  # 用于…（场景）
    "用来",
    # 中文「当…时」触发前导家族（多字，低误报）
    "当用户",  # 当用户…
    "当需要",  # 当需要…
    "需要时",  # …需要时
    "时使用",  # 当用户需要生成周报时使用
    "的情况下",  # …的情况下
    "情况时",  # …情况时
    # 英文 when-to-use
    "when to use",
    "use when",
    "use this when",
    "use it when",
    "when the user",
    "applies when",
    "applied when",
    "trigger",
    "situation",
)


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


def _when_to_use_present(contract: SkillContract) -> bool:
    """Heuristic: does the contract say *when* to use the skill?

    True when ``applicable_when`` declares any trigger, or the goal text carries
    one of :data:`_WHEN_TO_USE_TOKENS`. Conservative on purpose — see the token
    list's rationale.
    """
    if contract.applicable_when:
        return True
    text = (contract.goal or "").lower()
    return any(token in text for token in _WHEN_TO_USE_TOKENS)


def _contract_body_text(contract: SkillContract) -> str:
    """Compose the SKILL.md body text for volume estimation.

    Reuses T07's :func:`forgeflow.skills.segments.contract_body_text` (single
    source of truth for what counts as body) by shaping the :class:`SkillContract`
    into the segment mapping that helper consumes. No token estimate is
    re-implemented here.
    """
    return _segments.contract_body_text(
        {
            _segments.SEGMENT_PROCEDURE: {"steps": list(contract.procedure or [])},
            _segments.SEGMENT_POLICIES: {"constraints": list(contract.policies or [])},
            _segments.SEGMENT_TOOL_BINDINGS: {
                "tools": list(contract.tools or []),
                "scripts": [],
            },
            _segments.SEGMENT_EXAMPLES: {"examples": []},
        }
    )


def _t08_findings(contract: SkillContract) -> list[dict[str, Any]]:
    """INC46 T08 — additive quality findings (body volume + when-to-use).

    Both codes are **new**; no pre-existing code or severity changes. Neither is
    ``high``, so neither enters ``must_fix`` (the candidate still reaches the
    TESTING/REVIEW stages); the *blocking* decision for these lives in the T08
    candidate gate (:mod:`forgeflow.skills.candidate_gates`), which treats the
    critical ``medium`` codes as blocking — a finding alone never silently
    passes.

    * ``body_over_limit`` (``medium``) — the composable SKILL.md body exceeds the
      T07 recommended size (< 500 行 / < 5000 tokens, ``spec_validator``). The
      content is **never truncated**; the advisory findings are joined verbatim.
    * ``description_missing_when_to_use`` (``low``) — the goal states *what* but
      gives no *when to use* signal (see :func:`_when_to_use_present`). Heuristic
      and advisory by construction, so it stays ``low`` (误报可控).
    """
    findings: list[dict[str, Any]] = []

    volume = _segments.body_volume_findings(_contract_body_text(contract))
    if volume:
        findings.append(
            _finding(
                "body_over_limit",
                "medium",
                "正文体量超建议上限（未截断，仅提示渐进披露）：" + "；".join(volume),
                "body",
            )
        )

    if contract.goal and not _when_to_use_present(contract):
        findings.append(
            _finding(
                "description_missing_when_to_use",
                "low",
                "描述只说明「做什么」，缺少「何时用」信号（未声明 applicable_when 且"
                "目标文本无触发 / 场景词）",
                "goal",
            )
        )

    return findings


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

    # --- INC46 T08 — additive quality findings (T07-体量 + 何时用) ----------
    # ``body_over_limit`` (medium) 与 ``description_missing_when_to_use`` (low)
    # 是新增 code；既有 code / severity 一字未改。二者均非 high，故不进
    # ``must_fix``；其「阻断」判定由 T08 闸门（candidate_gates）承担。
    findings.extend(_t08_findings(contract))

    severity = _max_severity(findings)
    must_fix = [
        str(f["code"]) for f in findings if str(f.get("severity")) == "high"
    ]
    return SkillCritique(findings=findings, severity=severity, must_fix=must_fix)
