"""INC46 T28 — the **Agent main loop** package (Plan → Act → Observe → Replan).

This package adds the *intelligence layer* the platform was missing on top of
T12's skill subgraph: a **bounded, auditable** dynamic loop that plans, acts,
observes whether reality matched the plan, and replans when it did not — while
guaranteeing termination (红线 18) and never widening permission (红线 5).

Modules
-------
* :mod:`forgeflow.agent.plan` — the ``Plan`` value objects (``plan_id`` / steps /
  assumptions / success criteria / budget) and risk derivation.
* :mod:`forgeflow.agent.budget` — the four hard ceilings and the
  :class:`~forgeflow.agent.budget.BudgetTracker` (exhaustion is never success).
* :mod:`forgeflow.agent.loop_guard` — cycle detection on the same
  ``(tool, normalised-args)`` repeated with no progress.
* :mod:`forgeflow.agent.loop` — :class:`~forgeflow.agent.loop.AgentLoop`, the
  feature flag (:func:`~forgeflow.agent.loop.agent_loop_enabled`, default OFF)
  and the convenience entry :func:`~forgeflow.agent.loop.run_agent_loop`.

The package is import-safe and side-effect free: importing it never runs a loop,
never touches the environment beyond reading a flag at call time, and never
imports :mod:`forgeflow.runtime` eagerly (so it cannot form an import cycle with
the runtime that imports the agent).
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
from forgeflow.agent.loop import (
    AgentLoop,
    LoopResult,
    Observation,
    RunContext,
    agent_loop_enabled,
    run_agent_loop,
)
from forgeflow.agent.loop_guard import (
    DEFAULT_REPEAT_THRESHOLD,
    LoopGuard,
    LoopSignal,
    fingerprint,
    normalize_args,
)
from forgeflow.agent.plan import (
    Plan,
    PlanStep,
    has_risky_steps,
    new_plan,
    new_plan_id,
    plan_signature,
    risk_level,
)

__all__ = [
    # plan
    "Plan",
    "PlanStep",
    "new_plan",
    "new_plan_id",
    "plan_signature",
    "risk_level",
    "has_risky_steps",
    # budget
    "Budget",
    "BudgetBreach",
    "BudgetTracker",
    "check_budget",
    "DEFAULT_MAX_STEPS",
    "DEFAULT_MAX_REPLANS",
    "DEFAULT_WALL_TIME_SECONDS",
    "DEFAULT_MAX_TOKENS",
    # loop guard
    "LoopGuard",
    "LoopSignal",
    "fingerprint",
    "normalize_args",
    "DEFAULT_REPEAT_THRESHOLD",
    # loop
    "AgentLoop",
    "LoopResult",
    "Observation",
    "RunContext",
    "agent_loop_enabled",
    "run_agent_loop",
]
