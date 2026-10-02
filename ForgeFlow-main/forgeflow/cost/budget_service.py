"""Budget service — three-level budgets and the degrade decision (INC2 A1).

Levels are ``task > team > tenant``: every level that has a budget is fetched
and the **strictest** (smallest ``limit_amount``) binds, per architecture
§2.1 step 1 (``min_limit``).

  * ``ratio >= 1.00``            → ``exceeded`` → run **all** ``on_exceed`` actions
  * ``warn_ratio <= ratio < 1``  → ``warn``     → notify only, **no** degradation
  * ``ratio < warn_ratio``       → ``ok``

``warn_ratio`` defaults to the budget row's own value, falling back to
``Settings.cost_warn_ratio`` (0.800).
"""

from __future__ import annotations

import logging
from typing import Any

from forgeflow.config import get_settings
from forgeflow.cost.degrade import build_degrade_state
from forgeflow.cost.models import (
    DEFAULT_EXCEED_ACTIONS,
    WARN_ACTIONS,
    BudgetDecision,
    CostBudgetRecord,
)

logger = logging.getLogger(__name__)

__all__ = ["BudgetService", "LEVELS"]


# Evaluated most-specific first; ``None`` scope_id means "tenant level".
LEVELS: tuple[tuple[str, str], ...] = (("task", "task_id"), ("team", "team_id"), ("tenant", ""))


class BudgetService:
    """Reads budgets from the active repository and turns them into decisions."""

    def __init__(self, repository: Any | None = None, *, warn_ratio: float | None = None) -> None:
        self._warn_ratio = warn_ratio
        self._repository = repository

    @property
    def repository(self) -> Any:
        """Lazily resolved so importing this module never builds a repository."""
        if self._repository is None:
            from forgeflow.repositories.factory import get_cost_repository

            self._repository = get_cost_repository()
        return self._repository

    def _default_warn_ratio(self) -> float:
        if self._warn_ratio is not None:
            return float(self._warn_ratio)
        return float(get_settings().cost_warn_ratio)

    # -- writes --------------------------------------------------------------
    async def set_budget(
        self,
        tenant_id: str | None,
        scope: str,
        limit_amount: float,
        *,
        scope_id: str | None = None,
        warn_ratio: float | None = None,
        currency: str | None = None,
        on_exceed: list[str] | None = None,
    ) -> CostBudgetRecord:
        """Create or replace one budget (upsert on the natural key)."""
        settings = get_settings()
        existing = await self.repository.get(tenant_id, scope, scope_id)
        record = CostBudgetRecord(
            tenant_id=tenant_id,
            scope=scope,
            scope_id=scope_id,
            limit_amount=float(limit_amount),
            warn_ratio=float(warn_ratio if warn_ratio is not None else self._default_warn_ratio()),
            currency=currency or getattr(settings, "cost_currency", "CNY"),
            on_exceed=list(on_exceed or DEFAULT_EXCEED_ACTIONS),
            id=existing.id if existing else CostBudgetRecord().id,
            created_at=existing.created_at if existing else CostBudgetRecord().created_at,
        )
        return await self.repository.upsert(record)

    async def delete_budget(
        self, tenant_id: str | None, scope: str, scope_id: str | None = None
    ) -> bool:
        """Delete one budget (tenant-scoped, by natural key). True iff a row went.

        INC40 / B6 — wiring this route closes the loop: both cost repositories'
        ``delete`` moves from "有实现、零生产调用方" to a real end-to-end path
        (``delete_budget`` → ``repository.delete``), and ``set_budget`` (upsert)
        becomes reachable via ``POST /cost/budgets`` — so the budget domain is now
        "建 → 读 → 删" end-to-end instead of a read-only shell.
        """
        return bool(await self.repository.delete(tenant_id, scope, scope_id))

    async def list_budgets(
        self, tenant_id: str | None, scope: str | None = None
    ) -> list[CostBudgetRecord]:
        return await self.repository.list_by_scope(tenant_id, scope)

    # -- reads ---------------------------------------------------------------
    async def effective_budgets(
        self,
        tenant_id: str | None,
        *,
        team_id: str | None = None,
        task_id: str | None = None,
    ) -> list[CostBudgetRecord]:
        """Every budget that applies, most-specific level first."""
        found: list[CostBudgetRecord] = []
        for scope, _arg in LEVELS:
            scope_id = {"task": task_id, "team": team_id, "tenant": None}[scope]
            if scope != "tenant" and not scope_id:
                continue
            budget = await self.repository.get(tenant_id, scope, scope_id)
            if budget is not None:
                found.append(budget)
        return found

    async def evaluate_budget(
        self,
        tenant_id: str | None,
        spent_usd: float,
        *,
        team_id: str | None = None,
        task_id: str | None = None,
    ) -> BudgetDecision:
        """Grade ``spent_usd`` against the strictest applicable budget."""
        spent = float(spent_usd or 0.0)
        candidates = await self.effective_budgets(
            tenant_id, team_id=team_id, task_id=task_id
        )
        if not candidates:
            # No budget configured ⇒ nothing to enforce, and nothing to claim.
            return BudgetDecision(
                level="ok",
                actions=(),
                remaining=0.0,
                limit=0.0,
                ratio=0.0,
                spent=spent,
                scope="none",
                has_budget=False,
                notify_only=True,
            )

        binding = min(candidates, key=lambda b: b.limit_amount)
        limit = float(binding.limit_amount or 0.0)
        if limit > 0:
            ratio = spent / limit
        else:
            # A zero limit forbids any spend: only a zero spend is "ok".
            ratio = 0.0 if spent <= 0 else 1.0

        warn_ratio = float(binding.warn_ratio or 0.0) or self._default_warn_ratio()

        if ratio >= 1.0:
            level = "exceeded"
            actions = tuple(binding.on_exceed or DEFAULT_EXCEED_ACTIONS)
            notify_only = False
        elif ratio >= warn_ratio:
            # Warn records + notifies but must NOT degrade (§2.1 step 3).
            level = "warn"
            actions = tuple(WARN_ACTIONS)
            notify_only = True
        else:
            level = "ok"
            actions = ()
            notify_only = True

        decision = BudgetDecision(
            level=level,
            actions=actions,
            remaining=max(0.0, limit - spent),
            limit=limit,
            ratio=ratio,
            spent=spent,
            scope=binding.scope,
            has_budget=True,
            notify_only=notify_only,
        )
        if level == "exceeded":
            logger.warning(
                "budget exceeded | tenant=%s scope=%s ratio=%.3f actions=%s",
                tenant_id,
                binding.scope,
                ratio,
                list(actions),
            )
        return decision

    # Convenience alias — both names are used in the design docs.
    evaluate = evaluate_budget

    def degrade_state(self, decision: BudgetDecision) -> Any:
        """Turn a decision's actions into a concrete :class:`DegradeState`."""
        return build_degrade_state(
            decision.actions,
            reason=f"budget {decision.level} ({decision.scope})",
            context={"ratio": decision.ratio, "limit": decision.limit},
        )
