"""INC46 T28 — the Agent Loop's execution **budget** (红线 18).

A Plan–Act–Observe–Replan loop must have an explicit, hard budget and a guaranteed
termination. This module owns exactly that: the four ceilings an Agent Loop run is
allowed to spend, and a stateful tracker that reports the **first** exceeded
ceiling deterministically.

Ceilings (initial defaults, per the task book TABLE 36)
-------------------------------------------------------
===========================  =========  =============================================
dimension                    default    note
===========================  =========  =============================================
``max_steps``                25         declared tool executions per loop run
``max_replans``              3          replan cycles allowed after a mismatch
``wall_time_seconds``        300.0      wall-clock ceiling for the whole loop
``max_tokens``               200_000    token ceiling (configurable via Settings)
===========================  =========  =============================================

Design rules
------------
* **Exhaustion is never success.** :meth:`BudgetTracker.check` returning a
  :class:`BudgetBreach` tells the loop to stop with ``status="budget_exceeded"`` —
  the caller must **not** fold that into a success (红线 18). The loop reports the
  completed / unfinished lists alongside, so "how far it got" stays visible.
* **Pure policy, thin state.** :func:`check_budget` takes plain numbers and returns
  the first breach in a fixed dimension order, so the policy is trivially
  unit-testable and deterministic; :class:`BudgetTracker` is the stateful wrapper
  the loop uses (mirrors :class:`forgeflow.validation.loop_breaker.LoopBreaker`).
* **A negative ceiling disables that dimension**; ``0`` is a *zero budget* (trips
  immediately), never "off" — reading ``0`` as "disabled" would silently lift the
  guard, which is strictly worse than one loud breach.
* **Clock is injectable** so wall-time behaviour is testable without sleeping.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

__all__ = [
    "DEFAULT_MAX_STEPS",
    "DEFAULT_MAX_REPLANS",
    "DEFAULT_WALL_TIME_SECONDS",
    "DEFAULT_MAX_TOKENS",
    "BUDGET_DIMENSIONS",
    "Budget",
    "BudgetBreach",
    "check_budget",
    "BudgetTracker",
]

#: Initial defaults (task book TABLE 36 — "待校准"; T36 re-calibrates them).
DEFAULT_MAX_STEPS = 25
DEFAULT_MAX_REPLANS = 3
DEFAULT_WALL_TIME_SECONDS = 300.0
DEFAULT_MAX_TOKENS = 200_000

#: Evaluated in this order; the first exceeded dimension is reported, which keeps
#: the verdict deterministic when several ceilings are crossed at once.
BUDGET_DIMENSIONS: tuple[str, ...] = ("steps", "replans", "seconds", "tokens")


@dataclass(frozen=True)
class Budget:
    """The four ceilings an Agent Loop run must not cross.

    ``max_tokens`` is the "token 预算可配" knob: :meth:`from_settings` reads it
    from ``Settings`` when present, otherwise falls back to the default. A
    **negative** value disables that single dimension (see :func:`check_budget`).
    """

    max_steps: int = DEFAULT_MAX_STEPS
    max_replans: int = DEFAULT_MAX_REPLANS
    wall_time_seconds: float = DEFAULT_WALL_TIME_SECONDS
    max_tokens: int = DEFAULT_MAX_TOKENS

    @classmethod
    def from_settings(cls) -> "Budget":
        """Build from ``Settings`` defensively.

        An older / stripped-down Settings object (or a test double) must never
        raise inside the run loop: a missing field falls back to its default.
        Reads ``agent_loop_*`` settings when present, else the module defaults.
        """
        try:
            from forgeflow.config import get_settings

            settings = get_settings()
        except Exception:  # noqa: BLE001 — a settings read must never break a loop
            return cls()
        return cls(
            max_steps=int(getattr(settings, "agent_loop_max_steps", DEFAULT_MAX_STEPS) or DEFAULT_MAX_STEPS),
            max_replans=int(
                getattr(settings, "agent_loop_max_replans", DEFAULT_MAX_REPLANS)
                if getattr(settings, "agent_loop_max_replans", DEFAULT_MAX_REPLANS) is not None
                else DEFAULT_MAX_REPLANS
            ),
            wall_time_seconds=float(
                getattr(settings, "agent_loop_wall_time_seconds", DEFAULT_WALL_TIME_SECONDS)
                or DEFAULT_WALL_TIME_SECONDS
            ),
            max_tokens=int(getattr(settings, "agent_loop_max_tokens", DEFAULT_MAX_TOKENS) or DEFAULT_MAX_TOKENS),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BudgetBreach:
    """One exceeded ceiling, with the numbers that prove it (红线 18)."""

    dimension: str
    used: float
    limit: float
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def check_budget(
    budget: Budget,
    *,
    steps: int = 0,
    replans: int = 0,
    seconds: float = 0.0,
    tokens: int = 0,
) -> BudgetBreach | None:
    """Return the first exceeded ceiling, or ``None`` while inside budget.

    Pure: same inputs ⇒ same answer, no clock read. A **negative** limit disables
    that dimension; ``0`` is a zero budget and trips immediately (``used >= 0``).
    """
    usage: dict[str, float] = {
        "steps": float(steps),
        "replans": float(replans),
        "seconds": float(seconds),
        "tokens": float(tokens),
    }
    limits: dict[str, float] = {
        "steps": float(budget.max_steps),
        "replans": float(budget.max_replans),
        "seconds": float(budget.wall_time_seconds),
        "tokens": float(budget.max_tokens),
    }
    for dimension in BUDGET_DIMENSIONS:
        used = usage[dimension]
        limit = limits[dimension]
        if limit >= 0 and used >= limit:
            return BudgetBreach(
                dimension=dimension,
                used=used,
                limit=limit,
                message=(
                    f"Agent Loop 预算耗尽：{dimension} 已用 {used:g} ≥ 上限 {limit:g}"
                    "——终止循环，判为 budget_exceeded（不折算成功，红线 18）"
                ),
            )
    return None


class BudgetTracker:
    """Stateful budget accounting for **one** Agent Loop run.

    The loop calls :meth:`check` before each action and :meth:`note_step` /
    :meth:`note_replan` / :meth:`note_tokens` after. The instance keeps the audit
    trail (every distinct breach, plus the live counters) so a reader can tell
    "the budget was never consulted" apart from "it was consulted and found
    nothing wrong" — the same no-data-vs-zero distinction the SLO module makes.
    """

    def __init__(self, budget: Budget | None = None, *, clock: Any | None = None) -> None:
        self.budget = budget or Budget()
        self._clock = clock or time.monotonic
        self._start = self._clock()
        self.steps = 0
        self.replans = 0
        self.tokens = 0
        self.breaches: list[BudgetBreach] = []
        self.observations = 0

    # -- accounting --------------------------------------------------------- #
    def elapsed(self) -> float:
        """Seconds since the tracker was created (uses the injected clock)."""
        try:
            return float(self._clock() - self._start)
        except Exception:  # noqa: BLE001 — a clock hiccup must not break a loop
            return 0.0

    def note_step(self, n: int = 1) -> None:
        self.steps += max(0, int(n))

    def note_replan(self, n: int = 1) -> None:
        self.replans += max(0, int(n))

    def note_tokens(self, n: int = 0) -> None:
        self.tokens += max(0, int(n or 0))

    # -- decision ----------------------------------------------------------- #
    def check(self) -> BudgetBreach | None:
        """Record one observation and return the active breach (if any)."""
        self.observations += 1
        breach = check_budget(
            self.budget,
            steps=self.steps,
            replans=self.replans,
            seconds=self.elapsed(),
            tokens=self.tokens,
        )
        if breach is not None and all(b.dimension != breach.dimension for b in self.breaches):
            self.breaches.append(breach)
        return breach

    def would_allow_step(self) -> bool:
        """Whether one more step fits the budget (does not record an observation)."""
        return check_budget(
            self.budget,
            steps=self.steps + 1,
            replans=self.replans,
            seconds=self.elapsed(),
            tokens=self.tokens,
        ) is None

    def would_allow_replan(self) -> bool:
        """Whether one more replan fits the budget (does not record an observation)."""
        return check_budget(
            self.budget,
            steps=self.steps,
            replans=self.replans + 1,
            seconds=self.elapsed(),
            tokens=self.tokens,
        ) is None

    @property
    def tripped(self) -> bool:
        return bool(self.breaches)

    def to_dict(self) -> dict[str, Any]:
        """Serialisable audit trail — lands on the loop result as ``budget``."""
        return {
            "budget": self.budget.to_dict(),
            "observations": self.observations,
            "steps": self.steps,
            "replans": self.replans,
            "tokens": self.tokens,
            "elapsed_seconds": self.elapsed(),
            "tripped": self.tripped,
            "breaches": [b.to_dict() for b in self.breaches],
        }
