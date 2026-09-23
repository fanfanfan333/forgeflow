"""Cost optimization (INC2 A1) — budgets, degrade actions and savings.

Split by responsibility so each file stays small and independently testable:

  * :mod:`forgeflow.cost.models`      — domain records / value objects
  * :mod:`forgeflow.cost.budget_service` — three-level budget evaluation
  * :mod:`forgeflow.cost.degrade`     — what each degrade action actually does
  * :mod:`forgeflow.cost.savings`     — savings vs. the previous period
  * :mod:`forgeflow.cost.ledger`      — the per-run spend source behind ``/cost/*``

Zero LLM dependency: every decision here is arithmetic over recorded costs.
"""

from __future__ import annotations

from forgeflow.cost.models import (
    BUDGET_SCOPES,
    DEFAULT_EXCEED_ACTIONS,
    WARN_ACTIONS,
    BudgetDecision,
    CostBudgetRecord,
    SavingsResult,
)

__all__ = [
    "BUDGET_SCOPES",
    "DEFAULT_EXCEED_ACTIONS",
    "WARN_ACTIONS",
    "BudgetDecision",
    "CostBudgetRecord",
    "SavingsResult",
]
