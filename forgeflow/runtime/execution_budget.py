"""Server-owned execution limits shared by replans and delegated work."""

from __future__ import annotations

import math
import time
from contextvars import ContextVar
from dataclasses import dataclass, field


class BudgetExceeded(RuntimeError):
    """Execution stopped before another operation could start."""


@dataclass
class ExecutionBudget:
    model_limit: int = 24
    tool_limit: int = 64
    seconds_limit: float = 600
    model_calls: int = 0
    tool_calls: int = 0
    delegations: int = 0
    started: float = field(default_factory=time.monotonic)
    failure_receipts: dict[tuple, tuple[str, int]] = field(default_factory=dict, repr=False)
    evidence_receipts: dict[tuple, set[str]] = field(default_factory=dict, repr=False)

    @property
    def remaining_seconds(self) -> float:
        remaining = self.seconds_limit - (time.monotonic() - self.started)
        if remaining <= 0:
            raise BudgetExceeded("budget_exceeded: 任务执行时间达到服务端上限")
        return remaining

    def consume(self, kind: str) -> None:
        _ = self.remaining_seconds
        if kind not in {"model", "tool", "delegation"}:
            raise ValueError("Unknown execution budget kind")
        attribute = {"model": "model_calls", "tool": "tool_calls", "delegation": "delegations"}[
            kind
        ]
        limit = {"model": self.model_limit, "tool": self.tool_limit, "delegation": 4}[kind]
        if getattr(self, attribute) >= limit:
            raise BudgetExceeded(f"budget_exceeded: {kind} 调用达到服务端上限 {limit}")
        setattr(self, attribute, getattr(self, attribute) + 1)

    @classmethod
    def restore(cls, snapshot: dict) -> ExecutionBudget:
        """Restore only server-stored checkpoints; legacy time usage is unknown."""
        if not isinstance(snapshot, dict):
            raise ValueError("Invalid budget checkpoint")
        budget = cls()
        for name in ("model_calls", "tool_calls", "delegations"):
            value = snapshot.get(name)
            if type(value) is not int or value < 0:
                raise ValueError("Invalid budget usage")
            setattr(budget, name, value)
        for name in ("model_limit", "tool_limit", "seconds_limit"):
            value = snapshot.get(name)
            if type(value) not in (int, float) or value <= 0 or not math.isfinite(value):
                raise ValueError("Invalid budget ceiling")
            if name != "seconds_limit" and type(value) is not int:
                raise ValueError("Call ceilings must be integers")
            setattr(budget, name, min(value, getattr(budget, name)))
        elapsed = snapshot.get("elapsed_seconds")
        if type(elapsed) not in (int, float) or elapsed < 0 or not math.isfinite(elapsed):
            raise ValueError("Checkpoint has no valid measured execution time")
        budget.started = time.monotonic() - elapsed
        return budget

    def snapshot(self) -> dict:
        return {
            "elapsed_seconds": max(0.0, time.monotonic() - self.started),
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "delegations": self.delegations,
            "model_limit": self.model_limit,
            "tool_limit": self.tool_limit,
            "seconds_limit": self.seconds_limit,
        }


current_budget: ContextVar[ExecutionBudget | None] = ContextVar("execution_budget", default=None)
