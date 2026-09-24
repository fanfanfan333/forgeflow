"""Skill release gate — the "old vs new" check Skill CI/CD was missing.

What already existed
--------------------
``governance_gate.promote_candidate`` requires a **passing** evaluation before a
candidate becomes a skill version. That is an *absolute* bar: a new version
scoring 0.61 passes exactly as loudly as one scoring 0.99, so a **regression**
against the version it supersedes walks straight through the gate. The platform
already had a tolerance table for this (``forgeflow.evaluation.regression``), but
only ``scripts/run_eval.py`` ever called it — so skill releases were the one place
the policy was *not* applied.

What this adds
--------------
``evaluate_release`` turns the recorded metrics of the version being superseded
into a baseline and compares the candidate's recorded metrics against it, reusing
the platform's single tolerance policy (extended with the skill's own ``score``
dimension). It is deliberately **offline and deterministic** — recorded metrics
only, no LLM, no eval-suite execution at promote time; running a suite inside a
governance gate would make promotion latency unbounded.

Severity ladder (worst wins)
----------------------------
``no_baseline`` → nothing to compare (a first release, **or** a baseline that
                  shares no comparable metric with the candidate) ⇒ allowed
``ok``          → within tolerance                    ⇒ allowed
``warning``     → slipped past the warn tolerance      ⇒ allowed, but recorded
``regression``  → slipped past the fail tolerance      ⇒ **403**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from forgeflow.evaluation.regression import (
    TOLERANCES,
    RegressionFinding,
    check_dict,
)

#: The agent-eval tolerance table plus the Skill Hub's own composite score.
#: ``score`` is what ``skills/evaluator.py`` writes (and what
#: ``SkillVersionRecord.eval_score`` persists per version), so it is the one
#: metric a release gate can always compare against the previous version.
RELEASE_TOLERANCES: dict[str, tuple[str, float, float]] = {
    **TOLERANCES,
    "score": ("higher_is_better", 0.02, 0.05),
}

#: Order matters: used to pick the worst severity.
_SEVERITY_ORDER: tuple[str, ...] = ("no_baseline", "ok", "warning", "regression")


@dataclass(frozen=True)
class ReleaseDecision:
    """Verdict of the release gate for one promotion attempt."""

    allowed: bool
    severity: str
    reason: str
    baseline_present: bool = False
    findings: tuple[RegressionFinding, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "severity": self.severity,
            "reason": self.reason,
            "baseline_present": self.baseline_present,
            "findings": [
                {
                    "metric": f.metric,
                    "current": f.current,
                    "baseline": f.baseline,
                    "delta": f.delta,
                    "severity": f.severity,
                    "explanation": f.explanation,
                }
                for f in self.findings
            ],
        }


def _worst(severities: list[str]) -> str:
    """Return the most severe entry of ``severities`` (``ok`` when empty)."""
    ranked = [s for s in severities if s in _SEVERITY_ORDER]
    if not ranked:
        return "ok"
    return max(ranked, key=_SEVERITY_ORDER.index)


def evaluate_release(
    new_metrics: dict[str, Any] | None,
    baseline_metrics: dict[str, Any] | None,
) -> ReleaseDecision:
    """Decide whether ``new_metrics`` may be released over ``baseline_metrics``.

    Pure and side-effect free: no persistence, no clock, no LLM. A missing or
    empty baseline is reported as ``no_baseline`` (allowed) rather than silently
    as ``ok``, so a caller can tell "verified clean" from "nothing to verify".

    The same "nothing to verify" verdict — ``no_baseline`` but with
    ``baseline_present=True`` — is returned when a baseline **exists** yet shares
    no comparable metric with the candidate. Reporting a clean ``ok`` there (the
    old behaviour, via ``_worst([]) == "ok"``) would let "never compared"
    masquerade as "verified clean", which is exactly the confusion this gate
    exists to prevent.
    """
    if not baseline_metrics:
        return ReleaseDecision(
            allowed=True,
            severity="no_baseline",
            reason="无历史基线（首次发布）——放行",
            baseline_present=False,
        )

    report = check_dict(new_metrics, baseline_metrics, tolerances=RELEASE_TOLERANCES)

    # Baseline present but zero overlapping metrics ⇒ nothing was compared.
    # Surface it as the "no_baseline" severity (same "nothing to compare"
    # family, so the audit severity set is unchanged) while ``baseline_present``
    # stays True and the reason states the truth — an honest, distinguishable
    # conclusion instead of a bogus "与上一版本持平或更优".
    if not report.findings:
        return ReleaseDecision(
            allowed=True,
            severity="no_baseline",
            reason="基线存在但无可比指标——未做比较",
            baseline_present=True,
        )

    severity = _worst([f.severity for f in report.findings])

    if severity == "regression":
        worst = [
            f for f in report.findings if f.severity == "regression"
        ]
        detail = "；".join(f.explanation for f in worst)
        return ReleaseDecision(
            allowed=False,
            severity=severity,
            reason=f"新版本相对上一版本出现质量回归（{detail}）",
            baseline_present=True,
            findings=tuple(report.findings),
        )

    if severity == "warning":
        detail = "；".join(
            f.explanation for f in report.findings if f.severity == "warning"
        )
        return ReleaseDecision(
            allowed=True,
            severity=severity,
            reason=f"放行，但质量指标已下滑（{detail}）",
            baseline_present=True,
            findings=tuple(report.findings),
        )

    return ReleaseDecision(
        allowed=report.passed,
        severity=severity,
        reason="与上一版本持平或更优",
        baseline_present=True,
        findings=tuple(report.findings),
    )


def baseline_from_version(version: Any) -> dict[str, float]:
    """Baseline metrics recoverable from the version being superseded.

    ``SkillVersionRecord`` persists ``eval_score`` per version but not the full
    metric dict (the other dimensions live on the *candidate's*
    ``SkillEvaluationRecord``). We therefore return exactly what the stored data
    can prove — the composite ``score`` — instead of inventing the rest. An
    absent score yields ``{}``, which :func:`evaluate_release` reports as
    ``no_baseline``: honest, not silently permissive.
    """
    if version is None:
        return {}
    score = getattr(version, "eval_score", None)
    if score is None:
        return {}
    try:
        return {"score": float(score)}
    except (TypeError, ValueError):
        return {}


__all__ = [
    "RELEASE_TOLERANCES",
    "ReleaseDecision",
    "baseline_from_version",
    "evaluate_release",
]
