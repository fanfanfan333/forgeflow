"""Loop breaker — the Agent Loop's *budget* ceiling (architecture review #10).

:func:`forgeflow.validation.replan.decide_replan` already owns the **attempt
count** ceiling (``Settings.max_replan_attempts``). That alone is not enough in
production, because a *single* attempt can burn an unbounded number of tokens or
wall-clock seconds: a pathological plan stays "inside the retry ceiling" while
the cost runs away. This module adds exactly the two dimensions that were
missing — cumulative tokens and elapsed time — and nothing else.

Deliberate scope
----------------
The attempt ceiling is **not** duplicated here. Two sources for one limit is how
"the ceiling is 2" and "the ceiling is 3" end up in the same system; the count
lives in :mod:`forgeflow.validation.replan` and the budget lives here. The
runtime consults the breaker only when ``decide_replan`` has *already* decided to
retry, so the breaker can only veto a retry — it can never authorise one.

Design rules
------------
* :func:`check_budget` is a **pure function** — no clock, no state, deterministic
  dimension ordering — so the policy is trivially unit-testable.
* :class:`LoopBreaker` is the thin *stateful* wrapper the runtime uses. It keeps
  the audit trail (every breach, plus how many times it was consulted) that is
  surfaced on the run, so "we stopped because X" is provable after the fact.
* **Fail closed, never silently.** A breach always escalates to HITL; it is never
  an implicit abort and never a licence to keep retrying.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

#: Evaluated in this order; the first exceeded dimension is reported, which keeps
#: the verdict deterministic when both ceilings are crossed at once.
DIMENSIONS: tuple[str, ...] = ("tokens", "seconds")


@dataclass(frozen=True)
class LoopBudget:
    """Budget ceilings the Agent Loop must not cross.

    Both come from ``Settings`` (``max_run_tokens`` / ``max_run_seconds``); the
    defaults are deliberately generous so the offline profile is unaffected.
    """

    max_tokens: int = 200_000
    max_seconds: float = 600.0

    @classmethod
    def from_settings(cls) -> LoopBudget:
        """Build from ``Settings`` — defensively, so an older Settings object
        (or a stripped-down test double) still yields a usable budget instead of
        raising inside the run loop."""
        from forgeflow.config import get_settings

        settings = get_settings()
        return cls(
            max_tokens=int(getattr(settings, "max_run_tokens", 200_000) or 200_000),
            max_seconds=float(getattr(settings, "max_run_seconds", 600.0) or 600.0),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BudgetBreach:
    """One exceeded ceiling, with the numbers that prove it."""

    dimension: str
    used: float
    limit: float
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def check_budget(
    budget: LoopBudget,
    *,
    tokens: int,
    seconds: float,
) -> BudgetBreach | None:
    """Return the first exceeded ceiling, or ``None`` while inside budget.

    Pure: same inputs ⇒ same answer, no clock read. A **negative** limit disables
    that dimension — that is how an operator turns one ceiling off without
    removing it. ``0`` is **not** "off": it is a *zero budget*, so the ceiling
    trips immediately even when usage is ``0`` (``used >= 0`` always holds).

    That asymmetry is deliberate and **fail-closed**: reading ``0`` as "disable"
    would silently lift the budget protection, which is far worse than one loud
    breach (the module's own rule is "Fail closed, never silently"). Only a
    strictly negative limit removes a dimension.
    """
    usage: dict[str, float] = {"tokens": float(tokens), "seconds": float(seconds)}
    limits: dict[str, float] = {
        "tokens": float(budget.max_tokens),
        "seconds": float(budget.max_seconds),
    }
    for dimension in DIMENSIONS:
        used = usage[dimension]
        limit = limits[dimension]
        if limit >= 0 and used >= limit:
            return BudgetBreach(
                dimension=dimension,
                used=used,
                limit=limit,
                message=(
                    f"Agent Loop 预算熔断：{dimension} 已用 {used:g} ≥ 上限 {limit:g}"
                    "——停止重规划并转人工（HITL）"
                ),
            )
    return None


class LoopBreaker:
    """Stateful budget guard for one run's replan loop.

    The runtime calls :meth:`observe` right before a retry is executed. The
    instance also records the *observation count* so a reader can tell "the
    breaker was never consulted" apart from "it was consulted and found nothing
    wrong" — the same no-data-vs-zero distinction the SLO module makes.
    """

    def __init__(self, budget: LoopBudget | None = None) -> None:
        self.budget = budget or LoopBudget.from_settings()
        self.breaches: list[BudgetBreach] = []
        self.observations = 0

    def observe(self, *, tokens: int, seconds: float) -> BudgetBreach | None:
        """Record one observation and return the active breach (if any).

        A dimension is recorded only the first time it breaches, so the audit
        trail reads as "which ceiling we hit", not "how many times we asked".
        """
        self.observations += 1
        breach = check_budget(self.budget, tokens=tokens, seconds=seconds)
        if breach is not None and all(
            b.dimension != breach.dimension for b in self.breaches
        ):
            self.breaches.append(breach)
        return breach

    @property
    def tripped(self) -> bool:
        return bool(self.breaches)

    def to_dict(self) -> dict[str, Any]:
        """Serialisable audit trail — lands on the run as ``detail["loop"]``."""
        return {
            "budget": self.budget.to_dict(),
            "observations": self.observations,
            "tripped": self.tripped,
            "breaches": [b.to_dict() for b in self.breaches],
        }


__all__ = [
    "DIMENSIONS",
    "BudgetBreach",
    "LoopBreaker",
    "LoopBudget",
    "check_budget",
]
