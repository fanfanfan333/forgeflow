"""BE-3 jump ⑦ — deterministic repair + ``SkillRevision`` record.

``repair`` fixes the *named, fixable* defects a critique (④) or a test run (⑤)
reported, and returns a repaired copy of the contract plus a
:class:`~forgeflow.skills.contracts.SkillRevision` describing the change.

Guarantees:

* **Deterministic / offline** — the same ``(contract, critique, evaluation,
  round)`` always yields the same result. No LLM, no I/O.
* **No fabrication of success** — repair only fills *structural gaps* with
  honest, explicit defaults (a goal derived from the declared procedure, a
  minimal declared I/O, a real weak verification assertion). It never strips a
  check, never marks anything "passed", and never rewrites a *declared* value
  that contradicts the critique — the re-run's verdict stays honest.
* **Semver is *proposed*, never overwritten** — the loop is pre-promotion, so
  there is no stored version yet. ``from_semver`` / ``to_semver`` describe the
  *prospective* next patch version (round 1 ⇒ ``0.1.0 → 0.1.1`` …) and are
  computed with ``versioning.bump_semver``. No existing ``current_version`` is
  ever touched (promotion owns that, via ``governance_gate``).
* The structural diff reuses ``versioning.diff_specs`` verbatim.
"""

from __future__ import annotations

from forgeflow.skills.contracts import (
    MEDIUM_RISK,
    SkillContract,
    SkillCritique,
    SkillEvaluation,
    SkillRevision,
)
from forgeflow.skills.tester import (
    ASSERT_INPUT_KEYS_DECLARED,
    ASSERT_OUTPUT_DECLARED,
    ASSERT_STRUCTURALLY_VIABLE,
    ASSERT_TOOLS_WHITELISTED,
    ASSERT_VERIFICATION_DECLARED,
)
from forgeflow.skills.trust_baseline import allowed_tool_set
from forgeflow.skills.versioning import bump_semver, diff_specs

__all__ = ["repair", "BASE_SEMVER"]

#: Prospective base version for a first-cycle contract (no stored version yet).
BASE_SEMVER = "0.1.0"

#: Honest fallback tool ids (real platform tools) — reused from the compiler's
#: own fallback list, filtered to what the catalogue actually exposes.
_FALLBACK_TOOL_IDS = ("research.search", "data.query", "report.render")

#: Default (declared, minimal) I/O + verification used only to fill *gaps*.
_DEFAULT_INPUTS: dict[str, str] = {"intent": "string"}
_DEFAULT_OUTPUTS: dict[str, str] = {"summary": "string"}
_DEFAULT_VERIFICATION = ["输出非空"]


def _catalogue_tools() -> list[str]:
    """Real platform tool ids from the single source of truth (stable order)."""
    catalogue = allowed_tool_set()
    return [tool for tool in _FALLBACK_TOOL_IDS if tool in catalogue]


def _reconcile_tools(contract: SkillContract) -> None:
    """Drop off-whitelist tools; fall back to real catalogue tools if emptied."""
    catalogue = allowed_tool_set()
    kept = [t for t in contract.tools if t in catalogue]
    if not kept:
        kept = _catalogue_tools()
    contract.tools = kept


def _apply_fix(contract: SkillContract, code: str) -> bool:
    """Apply one named fix in place. Returns ``True`` iff something changed."""
    before = contract.to_dict()

    if code == "goal_missing":
        domain = str(contract.applicable_when.get("domain", "") or "").strip()
        subject = domain or (contract.procedure[0] if contract.procedure else "任务")
        contract.goal = f"完成「{subject}」并产出结论"
    elif code == "procedure_missing":
        contract.procedure = ["检索上下文", "执行任务", "生成结论"]
    elif code in ("tools_missing", "tools_not_whitelisted"):
        _reconcile_tools(contract)
    elif code in ("inputs_missing", ASSERT_INPUT_KEYS_DECLARED):
        contract.inputs = dict(contract.inputs or _DEFAULT_INPUTS)
    elif code in ("outputs_missing", ASSERT_OUTPUT_DECLARED):
        contract.outputs = dict(contract.outputs or _DEFAULT_OUTPUTS)
    elif code in ("verification_missing", "high_risk_unverified", ASSERT_VERIFICATION_DECLARED):
        contract.verification = list(contract.verification or _DEFAULT_VERIFICATION)
    elif code == "risk_invalid":
        contract.risk_level = MEDIUM_RISK  # unknown ⇒ conservative, never "low"
    elif code in ("tools_whitelisted", ASSERT_TOOLS_WHITELISTED):
        _reconcile_tools(contract)
    elif code in (ASSERT_STRUCTURALLY_VIABLE, "structurally_viable"):
        if not contract.goal:
            _apply_fix(contract, "goal_missing")
        if not contract.procedure:
            _apply_fix(contract, "procedure_missing")
        if not contract.tools:
            _apply_fix(contract, "tools_missing")
        if not contract.inputs:
            _apply_fix(contract, "inputs_missing")
        if not contract.outputs:
            _apply_fix(contract, "outputs_missing")
    elif code == "policies_missing":
        return False  # informational only — no fix required

    return contract.to_dict() != before


def _prospective_semvers(round_no: int) -> tuple[str, str]:
    """``(from_semver, to_semver)`` — prospective patch bump for this round."""
    from_semver = BASE_SEMVER
    for _ in range(max(0, round_no - 1)):
        from_semver = bump_semver(from_semver, "patch")
    return from_semver, bump_semver(from_semver, "patch")


def repair(
    contract: SkillContract,
    critique: SkillCritique | None,
    evaluation: SkillEvaluation | None,
    round: int = 1,
) -> tuple[SkillContract, SkillRevision]:
    """Repair ``contract`` against the critique + evaluation, return the revision.

    Args:
        contract: the contract to repair (a copy is returned; the input is left
            untouched).
        critique: the current critique (its ``must_fix`` codes are repaired).
        evaluation: the latest sandbox result (its ``failure_modes`` codes are
            repaired). May be ``None`` before the first test run.
        round: 1-based repair round (⇒ a 1-based prospective patch version).

    Returns:
        ``(repaired_contract, revision)`` where ``revision.diff_summary`` is the
        structural diff (``versioning.diff_specs``) from the *original* spec.
    """
    original = SkillContract.from_dict(contract.to_dict())

    codes: list[str] = []
    if critique is not None:
        codes.extend(str(c) for c in critique.must_fix)
    if evaluation is not None:
        codes.extend(str(m) for m in evaluation.failure_modes)
    # Stable, de-duplicated fix order.
    ordered: list[str] = []
    for code in codes:
        if code and code not in ordered:
            ordered.append(code)

    repaired = SkillContract.from_dict(contract.to_dict())
    applied: list[str] = []
    for code in ordered:
        if _apply_fix(repaired, code):
            applied.append(code)

    from_semver, to_semver = _prospective_semvers(round)
    diff = diff_specs(original.to_draft_spec(), repaired.to_draft_spec())
    reason = (
        f"第 {round} 轮修复：" + ("、".join(applied) if applied else "无可自动修复项")
    )
    revision = SkillRevision(
        from_semver=from_semver,
        to_semver=to_semver,
        reason=reason,
        diff_summary=diff,
        round=round,
    )
    return repaired, revision
