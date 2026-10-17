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
    reserved_model_calls: int = 0
    reserved_tool_calls: int = 0
    started: float = field(default_factory=time.monotonic)
    _code_lease: dict[str, int] | None = field(default=None, init=False, repr=False)
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
        reserved = getattr(self, f"reserved_{attribute}", 0)
        if getattr(self, attribute) + reserved >= limit:
            raise BudgetExceeded(f"budget_exceeded: {kind} 调用达到服务端上限 {limit}")
        setattr(self, attribute, getattr(self, attribute) + 1)
        self._persist()

    def _persist(self) -> None:
        if current_budget.get() is self:
            from forgeflow.runtime.recovery import persist_budget

            persist_budget()

    def reserve_code(self) -> dict[str, int]:
        _ = self.remaining_seconds
        lease = {"model": self.model_limit - self.model_calls - self.reserved_model_calls,
                 "tool": self.tool_limit - self.tool_calls - self.reserved_tool_calls}
        if min(lease.values()) <= 0:
            raise BudgetExceeded("budget_exceeded: No call budget remains for the code runtime")
        self._code_lease = lease
        self.reserved_model_calls += lease["model"]
        self.reserved_tool_calls += lease["tool"]
        self._persist()
        return lease

    def settle_code(self, lease: dict[str, int], usage: dict) -> None:
        if lease is not self._code_lease or not isinstance(lease, dict) or not isinstance(usage, dict):
            raise ValueError("Code budget reservation is missing or already settled")
        for kind in ("model", "tool"):
            allocated = lease.get(kind)
            if type(allocated) is not int or not 0 < allocated <= getattr(self, f"reserved_{kind}_calls"):
                raise ValueError("Code budget reservation is missing or already settled")
            value = usage.get(kind)
            if type(value) is not int or not 0 <= value <= allocated:
                raise ValueError("Code runtime call usage is unverified; reservation retained")
        for kind in ("model", "tool"):
            setattr(self, f"reserved_{kind}_calls", getattr(self, f"reserved_{kind}_calls") - lease[kind])
            setattr(self, f"{kind}_calls", getattr(self, f"{kind}_calls") + usage[kind])

        self._code_lease = None
        self._persist()

    @classmethod
    def restore(cls, snapshot: dict) -> ExecutionBudget:
        """Restore only server-stored checkpoints; legacy time usage is unknown."""
        if not isinstance(snapshot, dict):
            raise ValueError("Invalid budget checkpoint")
        budget = cls()
        for name in ("model_calls", "tool_calls", "delegations", "reserved_model_calls", "reserved_tool_calls"):
            value = snapshot.get(name, 0) if name.startswith("reserved_") else snapshot.get(name)
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
            "reserved_model_calls": self.reserved_model_calls,
            "reserved_tool_calls": self.reserved_tool_calls,
            "tool_calls": self.tool_calls,
            "delegations": self.delegations,
            "model_limit": self.model_limit,
            "tool_limit": self.tool_limit,
            "seconds_limit": self.seconds_limit,
        }


current_budget: ContextVar[ExecutionBudget | None] = ContextVar("execution_budget", default=None)
