"""Replan decisioning (jump: Failure → Replan, docs §6.6 / P0-07).

A failed run is retried up to ``Settings.max_replan_attempts`` times; once the
ceiling is hit we stop replanning and flag the run for human approval (HITL).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from forgeflow.config import get_settings
from forgeflow.validation.validator import Verdict, _get  # noqa: PLC2701 — same package helper


@dataclass
class ReplanDecision:
    """Outcome of the replan gate for one attempt."""

    should_replan: bool
    attempt: int
    max_attempts: int
    escalate_hitl: bool
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def decide_replan(
    verdict: Verdict,
    attempt: int,
    *,
    max_attempts: int | None = None,
) -> ReplanDecision:
    """Decide whether to replan, and whether to escalate to a human.

    Args:
        verdict: the ``Verdict`` produced by ``validate``.
        attempt: how many replans have already happened (0 for the first run).
        max_attempts: override; defaults to ``Settings.max_replan_attempts``.
    """
    limit = max_attempts if max_attempts is not None else get_settings().max_replan_attempts

    if verdict.success:
        return ReplanDecision(False, attempt, limit, False, "run succeeded — no replan needed")

    if attempt < limit:
        return ReplanDecision(
            True,
            attempt,
            limit,
            False,
            f"attempt {attempt + 1}/{limit}: retrying after failure ({verdict.outcome})",
        )

    return ReplanDecision(
        False,
        attempt,
        limit,
        True,
        f"replan ceiling reached ({attempt}/{limit}) — escalating to HITL",
    )


def record_replan_event(decision: ReplanDecision) -> dict[str, Any]:
    """Serialise a replan decision into a RunEvent payload."""
    return {
        "event": "replan",
        "attempt": decision.attempt,
        "max_attempts": decision.max_attempts,
        "escalate_hitl": decision.escalate_hitl,
        "reason": decision.reason,
    }


def verdict_from(run: Any) -> Verdict:
    """Convenience: accept either a Verdict or a raw run."""
    if isinstance(run, Verdict):
        return run
    from forgeflow.validation.validator import validate

    return validate(run)


__all__ = [
    "ReplanDecision",
    "decide_replan",
    "record_replan_event",
    "verdict_from",
    "_get",
]
