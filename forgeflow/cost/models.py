"""Cost domain models (INC2 A1) — budgets, decisions and savings.

Mirrors the ``011`` migration's ``cost_budgets`` table one-to-one, and carries
the two value objects the Cost services return.

Design notes
------------
* ``CostBudgetRecord`` is backend-agnostic: the memory and PostgreSQL
  repositories both speak it, so switching ``STORAGE_BACKEND`` never changes a
  caller (repositories/base.py contract).
* ``scope`` ∈ ``tenant | team | task``. ``scope_id`` is NULL for tenant-level
  budgets and holds the team/task id otherwise — matching
  ``UNIQUE (tenant_id, scope, scope_id)``.
* Savings fields are **nullable on purpose** (architecture §7.1): when there is
  no previous-period baseline the API must return ``null`` so the SPA renders
  ``—``. It must never fall back to ``0`` or to the current period's cost.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from forgeflow.repositories.base import new_id, utcnow

__all__ = [
    "BUDGET_SCOPES",
    "DEFAULT_EXCEED_ACTIONS",
    "WARN_ACTIONS",
    "CostBudgetRecord",
    "BudgetDecision",
    "SavingsResult",
]

BUDGET_SCOPES: tuple[str, ...] = ("tenant", "team", "task")

# Applied, in order, when a budget is exceeded (ratio >= 1.0).
DEFAULT_EXCEED_ACTIONS: tuple[str, ...] = (
    "swap_model",
    "trim_context",
    "pause_noncritical",
)

# A warn never degrades — it only records + notifies (architecture §2.1 step 3).
WARN_ACTIONS: tuple[str, ...] = ("notify",)


def _normalise_scope(scope: str) -> str:
    """Clamp an unknown scope to ``tenant`` so a typo can't create a phantom."""
    candidate = (scope or "").strip().lower()
    return candidate if candidate in BUDGET_SCOPES else "tenant"


@dataclass
class CostBudgetRecord:
    """One row of ``cost_budgets``."""

    tenant_id: str | None = None
    scope: str = "tenant"
    scope_id: str | None = None
    limit_amount: float = 0.0
    warn_ratio: float = 0.8
    currency: str = "CNY"
    on_exceed: list[str] = field(default_factory=lambda: list(DEFAULT_EXCEED_ACTIONS))
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        self.scope = _normalise_scope(self.scope)
        if self.warn_ratio < 0.0:
            self.warn_ratio = 0.0
        elif self.warn_ratio > 1.0:
            self.warn_ratio = 1.0
        if not self.on_exceed:
            self.on_exceed = list(DEFAULT_EXCEED_ACTIONS)

    @property
    def key(self) -> tuple[str, str | None]:
        """Identity within a tenant — matches the table's UNIQUE constraint."""
        return (self.scope, self.scope_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "scope": self.scope,
            "scope_id": self.scope_id,
            "limit_amount": self.limit_amount,
            "warn_ratio": self.warn_ratio,
            "currency": self.currency,
            "on_exceed": list(self.on_exceed),
            "created_at": self.created_at.isoformat()
            if isinstance(self.created_at, datetime)
            else str(self.created_at),
        }


@dataclass(frozen=True)
class BudgetDecision:
    """Outcome of :meth:`BudgetService.evaluate_budget`."""

    level: str = "ok"  # ok | warn | exceeded
    actions: tuple[str, ...] = ()
    remaining: float = 0.0
    limit: float = 0.0
    ratio: float = 0.0
    spent: float = 0.0
    scope: str = "none"  # which budget bound the decision
    has_budget: bool = False
    notify_only: bool = True

    @property
    def is_exceeded(self) -> bool:
        return self.level == "exceeded"

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "actions": list(self.actions),
            "remaining": self.remaining,
            "limit": self.limit,
            "ratio": self.ratio,
            "spent": self.spent,
            "scope": self.scope,
            "has_budget": self.has_budget,
            "notify_only": self.notify_only,
        }


@dataclass(frozen=True)
class SavingsResult:
    """Savings vs. the previous period's like-for-like cost (§7.1).

    ``amount`` / ``baseline`` / ``delta_pct`` are ``None`` when there is no
    baseline — the SPA shows ``—``. Never ``0``, never the current cost.
    """

    amount: float | None = None
    baseline: float | None = None
    actual: float = 0.0
    delta_pct: float | None = None
    has_baseline: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "amount": self.amount,
            "baseline": self.baseline,
            "actual": self.actual,
            "delta_pct": self.delta_pct,
            "has_baseline": self.has_baseline,
        }
