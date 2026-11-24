"""Agent support package — the execution budget (FF-R1 B5 note).

This package originally also carried the INC46 T28 agent main
loop and its plan value objects; FF-G23 removed them and their
feature flag, and the cycle detector they shared with the
runtime moved to :mod:`forgeflow.runtime.loop_guard`. What
remains here is:

* :mod:`forgeflow.agent.budget` — the four hard ceilings and the
  :class:`~forgeflow.agent.budget.BudgetTracker` (exhaustion is
  never success; an explicitly configured non-positive ceiling is
  a configuration error, FF-G4).

The package is import-safe and side-effect free.
"""

from __future__ import annotations

from forgeflow.agent.budget import (
    DEFAULT_MAX_REPLANS,
    DEFAULT_MAX_STEPS,
    DEFAULT_MAX_TOKENS,
    DEFAULT_WALL_TIME_SECONDS,
    Budget,
    BudgetBreach,
    BudgetTracker,
    check_budget,
)

__all__ = [
    "Budget",
    "BudgetBreach",
    "BudgetTracker",
    "check_budget",
    "DEFAULT_MAX_STEPS",
    "DEFAULT_MAX_REPLANS",
    "DEFAULT_WALL_TIME_SECONDS",
    "DEFAULT_MAX_TOKENS",
]
