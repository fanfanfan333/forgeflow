"""Skill canary release — state machine + A/B decision + controlled exposure.

INC9 §B1 (``目标.md`` §6 ``P0-3``). The platform already had a full
"experience → candidate → evaluate → release-gate → promote" chain — but the
last hop was all-or-nothing: ``versioning.create_version`` pointed
``current_version`` straight at the new semver, so a new version took over
**instantly** with no measured window, no same-yardstick comparison, and no
automatic rollback.

What this module adds (and, just as importantly, what it does **not**)
--------------------------------------------------------------------
The platform has **no per-request serving/router layer for skills** — the
runtime graph (``runtime.orchestrator``) does not consume a skill spec at run
time. So "canary" here is modelled **honestly** as:

* a **release state machine** (:class:`CanaryState` / :func:`next_state`) that
  tracks a version as ``candidate`` → ``canary`` → ``promoted`` /
  ``rolled_back``;
* a **pure, deterministic A/B decision** (:func:`decide_ab`) over the *recorded*
  metrics of the two versions — reusing the platform's single tolerance policy
  (``RELEASE_TOLERANCES`` + ``check_dict``) so it shares one yardstick with the
  release gate rather than growing a second tolerance table;
* a **deterministic controlled-exposure** helper (:func:`should_serve`) for the
  *selection* layer (``SkillRegistry.select``), defaulting to ``0`` percent.

It deliberately does **not** fabricate a "traffic gateway / service mesh" or a
per-request outcome split: there is no per-version outcome ground truth to
compute, so the module states that boundary instead of inventing a number
(docs/sop/12-INC9-DESIGN.md §2.1.6).

Import safety: stdlib + two existing domain modules only — no I/O, no clock, no
asyncpg/langgraph/langchain at import time.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from forgeflow.evaluation.regression import RegressionFinding, check_dict
from forgeflow.skills.release_gate import RELEASE_TOLERANCES

__all__ = [
    "CanaryError",
    "CanaryState",
    "ABDecision",
    "next_state",
    "should_serve",
    "decide_ab",
]


class CanaryError(Exception):
    """Raised on an illegal canary state transition (fail-closed).

    A caller that asks for a transition the release state machine does not
    define gets a loud error rather than a silently-wrong state — the same
    fail-closed rule the RBAC middleware applies to unmapped routes.
    """


class CanaryState(str, Enum):
    """Release states of one skill version (INC9 §4.1).

    ``PROMOTED`` is the historical meaning of *every* version created via
    :func:`forgeflow.skills.versioning.create_version` (it took effect at once),
    which is why ``SkillVersionRecord.release_state`` defaults to ``"promoted"``.
    """

    CANDIDATE = "candidate"  # evaluated & passed, not yet released
    CANARY = "canary"  # released as a *non-default* candidate, under exposure
    PROMOTED = "promoted"  # promoted → became current_version
    ROLLED_BACK = "rolled_back"  # A/B verdict regressed → old version kept


#: Pure transition table (no clock, no I/O): ``(state, event) -> state``.
#: An event not listed for the current state is illegal → :class:`CanaryError`.
_CANARY_TRANSITIONS: dict[tuple[CanaryState, str], CanaryState] = {
    # canary disabled (default): an evaluated candidate promotes immediately.
    (CanaryState.CANDIDATE, "promote"): CanaryState.PROMOTED,
    # canary enabled: candidate first enters the exposure window.
    (CanaryState.CANDIDATE, "start_canary"): CanaryState.CANARY,
    # within the window: promote (win), rollback (regress), or hold (keep observing).
    (CanaryState.CANARY, "promote"): CanaryState.PROMOTED,
    (CanaryState.CANARY, "rollback"): CanaryState.ROLLED_BACK,
    (CanaryState.CANARY, "hold"): CanaryState.CANARY,
    # a human may still roll a live default back.
    (CanaryState.PROMOTED, "rollback"): CanaryState.ROLLED_BACK,
    # a rolled-back version may be re-promoted after a manual review.
    (CanaryState.ROLLED_BACK, "promote"): CanaryState.PROMOTED,
}


def next_state(current: CanaryState, event: str) -> CanaryState:
    """Return the state after ``event``; raise :class:`CanaryError` if illegal.

    Pure and total: same inputs always yield the same answer and an unknown
    ``event``/``current`` combination fails closed instead of guessing.
    """
    target = _CANARY_TRANSITIONS.get((current, event))
    if target is None:
        raise CanaryError(
            f"illegal canary transition: {current.value!r} -[{event}]-> ?"
        )
    return target


def should_serve(seed: str, pct: int) -> bool:
    """Deterministic controlled exposure for one ``(seed, pct)`` pair.

    ``sha1(seed) % 100 < pct`` — no clock, no RNG: the same ``seed`` always
    makes the same decision, so a request can be replayed. ``pct <= 0`` (the
    default) is **never** served, ``pct >= 100`` is **always** served, so with
    the default config the selection layer is byte-for-byte unchanged.

    This is deliberately *not* a per-request outcome splitter — there is no
    per-version outcome ground truth to compute (see the module docstring). It
    is the exposure primitive the *selection* layer uses.
    """
    try:
        pct_int = int(pct)
    except (TypeError, ValueError):
        return False
    if pct_int <= 0:
        return False
    if pct_int >= 100:
        return True
    digest = hashlib.sha1(str(seed or "").encode("utf-8")).hexdigest()
    return (int(digest, 16) % 100) < pct_int


#: Order matters: used to pick the worst severity (mirrors release_gate).
_SEVERITY_ORDER: tuple[str, ...] = ("no_baseline", "ok", "warning", "regression")


def _worst(severities: list[str]) -> str:
    """Return the most severe entry of ``severities`` (``ok`` when empty)."""
    ranked = [s for s in severities if s in _SEVERITY_ORDER]
    if not ranked:
        return "ok"
    return max(ranked, key=_SEVERITY_ORDER.index)


@dataclass(frozen=True)
class ABDecision:
    """Verdict of the post-canary A/B comparison (INC9 §4.1).

    ``action`` ∈ {``promote``, ``hold``, ``rollback``}; ``severity`` reuses the
    release gate's ladder (``no_baseline``/``ok``/``warning``/``regression``)
    plus ``insufficient`` (too few samples to conclude).
    """

    action: str
    severity: str
    reason: str
    sample_n: int
    baseline_present: bool
    findings: tuple[RegressionFinding, ...] = field(default_factory=tuple)

    @property
    def allowed(self) -> bool:
        """``True`` iff the decision promotes (the version becomes default)."""
        return self.action == "promote"

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "severity": self.severity,
            "reason": self.reason,
            "sample_n": self.sample_n,
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


def decide_ab(
    canary_metrics: dict[str, Any] | None,
    incumbent_metrics: dict[str, Any] | None,
    *,
    sample_n: int,
    min_samples: int,
    tolerances: dict[str, tuple[str, float, float]] = RELEASE_TOLERANCES,
) -> ABDecision:
    """Decide what to do with a canary version — **pure and deterministic**.

    Decision table (INC9 §2.1.4):

    ================================================  =========  ============
    condition                                         action     severity
    ================================================  =========  ============
    ``sample_n < min_samples``                        ``hold``   insufficient
    no comparable metrics / no baseline               ``hold``   no_baseline
    worst ``check_dict`` severity == ``regression``   ``rollback`` regression
    worst ∈ {``ok``, ``warning``} & enough samples    ``promote``  that level
    ================================================  =========  ============

    ``min_samples <= 0`` disables the sample floor. The tolerance table is the
    **same** one ``release_gate.evaluate_release`` uses, so an A/B verdict and a
    release-gate verdict never disagree about what "a regression" means.
    """
    baseline_present = bool(incumbent_metrics)
    sample_count = int(sample_n)

    if min_samples > 0 and sample_count < int(min_samples):
        return ABDecision(
            action="hold",
            severity="insufficient",
            reason=(
                f"样本不足（{sample_count} < {int(min_samples)}）——"
                "拒绝在噪声上下结论，保持观察"
            ),
            sample_n=sample_count,
            baseline_present=baseline_present,
        )

    if not incumbent_metrics:
        return ABDecision(
            action="hold",
            severity="no_baseline",
            reason="无历史基线可比——保持观察（『无可比』不等于『通过』）",
            sample_n=sample_count,
            baseline_present=False,
        )

    report = check_dict(canary_metrics, incumbent_metrics, tolerances=tolerances)
    if not report.findings:
        return ABDecision(
            action="hold",
            severity="no_baseline",
            reason="基线存在但无可比指标——未做比较，保持观察",
            sample_n=sample_count,
            baseline_present=True,
        )

    severity = _worst([f.severity for f in report.findings])
    findings = tuple(report.findings)

    if severity == "regression":
        detail = "；".join(
            f.explanation for f in report.findings if f.severity == "regression"
        )
        return ABDecision(
            action="rollback",
            severity=severity,
            reason=f"候选相对旧版质量回归（{detail}）——自动回滚，保留旧版",
            sample_n=sample_count,
            baseline_present=True,
            findings=findings,
        )

    if severity == "warning":
        return ABDecision(
            action="promote",
            severity=severity,
            reason="放行并提升为默认版本，但质量指标已下滑（已记录）",
            sample_n=sample_count,
            baseline_present=True,
            findings=findings,
        )

    return ABDecision(
        action="promote",
        severity=severity,
        reason="与旧版持平或更优——提升为默认版本",
        sample_n=sample_count,
        baseline_present=True,
        findings=findings,
    )
