"""Degrade actions — what a budget/SLO breach actually does (INC2 A1 + A2).

Actions are declarative strings so they can be stored on a budget row
(``cost_budgets.on_exceed``) or produced by an SLO tier, then turned into a
:class:`DegradeState` by :func:`build_degrade_state`.

  * ``notify``           — record + notify only. Never degrades.
  * ``swap_model``       — force the weak model (``get_model(strong=False)``).
  * ``trim_context``     — halve the Context Builder token budget (A5 hook).
  * ``pause_noncritical``— refuse *new* runs for non-core workflow types.

The module also owns the **degrade callback** registry: SLO breaches
(``observability/slo.py``) call :func:`trigger_degrade`, and any runtime piece
that wants to react registers a listener. Zero LLM dependency.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

__all__ = [
    "DEGRADE_ACTIONS",
    "CORE_WORKFLOW_TYPES",
    "CONTEXT_BUDGET_MULTIPLIER",
    "DegradeState",
    "build_degrade_state",
    "is_core_workflow",
    "register_degrade_callback",
    "clear_degrade_callbacks",
    "trigger_degrade",
]

DEGRADE_ACTIONS: tuple[str, ...] = (
    "notify",
    "swap_model",
    "trim_context",
    "pause_noncritical",
)

# Workflow types that keep running while ``pause_noncritical`` is in force.
# Everything else is refused (fast-fail) until the budget/SLO recovers.
CORE_WORKFLOW_TYPES: frozenset[str] = frozenset({"sales_ops", "agentflow_task"})

# How much of the normal context budget survives ``trim_context`` (§2.1 step 4).
CONTEXT_BUDGET_MULTIPLIER: float = 0.5


def is_core_workflow(workflow_type: str | None) -> bool:
    """True when ``workflow_type`` must keep running under ``pause_noncritical``."""
    return (workflow_type or "").strip().lower() in CORE_WORKFLOW_TYPES


@dataclass(frozen=True)
class DegradeState:
    """The resolved effect of a set of degrade actions."""

    actions: tuple[str, ...] = ()
    notified: bool = False
    use_weak_model: bool = False
    context_budget_multiplier: float = 1.0
    pause_noncritical: bool = False
    reason: str = ""
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def is_degraded(self) -> bool:
        return self.use_weak_model or self.pause_noncritical or (
            self.context_budget_multiplier < 1.0
        )

    def denies_new_run(self, workflow_type: str | None) -> bool:
        """Should a *new* run of ``workflow_type`` be refused right now?"""
        return self.pause_noncritical and not is_core_workflow(workflow_type)

    def effective_context_budget(self, budget_tokens: int) -> int:
        """Context Builder budget after ``trim_context`` (A5 hook)."""
        return max(1, int(budget_tokens * self.context_budget_multiplier))

    def to_dict(self) -> dict[str, Any]:
        return {
            "actions": list(self.actions),
            "notified": self.notified,
            "use_weak_model": self.use_weak_model,
            "context_budget_multiplier": self.context_budget_multiplier,
            "pause_noncritical": self.pause_noncritical,
            "reason": self.reason,
            "is_degraded": self.is_degraded,
        }


def build_degrade_state(
    actions: Any,
    *,
    reason: str = "",
    context: dict[str, Any] | None = None,
) -> DegradeState:
    """Translate a list of action names into a concrete :class:`DegradeState`.

    Unknown action names are ignored (and logged) rather than raising — a
    budget row written by hand must not be able to 500 the runtime.
    """
    raw = [str(a).strip() for a in (actions or []) if str(a).strip()]
    unknown = [a for a in raw if a not in DEGRADE_ACTIONS]
    if unknown:
        logger.warning("ignoring unknown degrade actions: %s", unknown)
    chosen = tuple(a for a in raw if a in DEGRADE_ACTIONS)
    return DegradeState(
        actions=chosen,
        notified="notify" in chosen,
        use_weak_model="swap_model" in chosen,
        context_budget_multiplier=(
            CONTEXT_BUDGET_MULTIPLIER if "trim_context" in chosen else 1.0
        ),
        pause_noncritical="pause_noncritical" in chosen,
        reason=reason,
        context=dict(context or {}),
    )


# --------------------------------------------------------------------------- #
# Degrade callbacks — how an SLO breach reaches the runtime                   #
# --------------------------------------------------------------------------- #

DegradeCallback = Callable[[str, tuple[str, ...], dict[str, Any]], None]

_CALLBACKS: list[DegradeCallback] = []


def register_degrade_callback(callback: DegradeCallback) -> None:
    """Register a listener invoked on every :func:`trigger_degrade`."""
    if callback not in _CALLBACKS:
        _CALLBACKS.append(callback)


def clear_degrade_callbacks() -> None:
    """Drop every listener. Test helper."""
    _CALLBACKS.clear()


def trigger_degrade(
    tier: str,
    actions: Any,
    *,
    reason: str = "",
    context: dict[str, Any] | None = None,
) -> DegradeState:
    """Fire the degrade actions for ``tier`` and notify every listener.

    A listener that raises is logged and skipped — an observability callback
    must never break the request that triggered it.
    """
    state = build_degrade_state(actions, reason=reason, context=context)
    if state.actions:
        logger.warning(
            "degrade triggered | tier=%s actions=%s reason=%s", tier, list(state.actions), reason
        )
    for callback in list(_CALLBACKS):
        try:
            callback(tier, state.actions, state.to_dict())
        except Exception as exc:  # noqa: BLE001 — callbacks must not break callers
            logger.error("degrade callback failed | tier=%s error=%s", tier, exc)
    return state
