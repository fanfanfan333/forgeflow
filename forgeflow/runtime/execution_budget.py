"""Server-owned execution limits shared by replans and delegated work."""

from __future__ import annotations

import math
import time
from contextvars import ContextVar
from dataclasses import dataclass, field


class BudgetExceeded(RuntimeError):
    """Execution stopped before another operation could start."""


#: What a parent always keeps for itself when a child budget is cut
#: from it (FF-R1 B3b): enough model/tool calls and wall-clock to
#: aggregate the children's results and produce the final answer.
#: The floor is never part of any child's share.
PARENT_RESERVE_MODEL_CALLS = 2
PARENT_RESERVE_TOOL_CALLS = 2
PARENT_RESERVE_SECONDS = 30.0


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
    _child_leases: list[tuple[ExecutionBudget, int]] = field(
        default_factory=list, init=False, repr=False
    )
    failure_receipts: dict[tuple, tuple[str, int]] = field(default_factory=dict, repr=False)
    evidence_receipts: dict[tuple, set[str]] = field(default_factory=dict, repr=False)
    # Tool circuit breaker (2026-10-31 upgrade round): consecutive
    # failures per (tenant, user, run, tool), regardless of arguments
    # or error text. The repeat-failure guard only stops the *same*
    # call a third time; a model flailing with different arguments
    # against a broken tool burned the whole tool budget. Runtime-only
    # like failure_receipts — snapshot/restore starts it fresh.
    circuit_failures: dict[tuple, int] = field(default_factory=dict, repr=False)

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

    def reserve_child(self, model_cap: int, tool_cap: int) -> ExecutionBudget | None:
        """Cut a child budget out of this budget's remaining calls.

        Follows the reserve_code/settle_code pattern: the child's
        limits join this budget's reservations up front, so the
        parent's own consume() can never spend them, and
        :meth:`settle_child` later charges back only what the child
        actually used. The parent's floor (``PARENT_RESERVE_*``) is
        never cut: after the child, the parent still holds at least
        2 model calls, 2 tool calls and ~30 seconds of its own.

        The child inherits the run's current delegation count, so the
        run-wide delegation cap (4) keeps counting grandchildren
        against the same total instead of resetting per child.

        Returns ``None`` when what remains above the floor cannot
        give a child even one model and one tool call — the caller
        must not spawn in that case.
        """
        if (
            type(model_cap) is not int
            or type(tool_cap) is not int
            or model_cap <= 0
            or tool_cap <= 0
        ):
            return None
        try:
            seconds_left = self.remaining_seconds
        except BudgetExceeded:
            return None
        model = min(
            model_cap,
            self.model_limit
            - self.model_calls
            - self.reserved_model_calls
            - PARENT_RESERVE_MODEL_CALLS,
        )
        tool = min(
            tool_cap,
            self.tool_limit
            - self.tool_calls
            - self.reserved_tool_calls
            - PARENT_RESERVE_TOOL_CALLS,
        )
        seconds = seconds_left - PARENT_RESERVE_SECONDS
        if model <= 0 or tool <= 0 or seconds <= 0:
            return None
        child = ExecutionBudget(model_limit=model, tool_limit=tool, seconds_limit=seconds)
        child.delegations = self.delegations
        self._child_leases.append((child, self.delegations))
        self.reserved_model_calls += model
        self.reserved_tool_calls += tool
        self._persist()
        return child

    def settle_child(self, child: ExecutionBudget) -> None:
        """Charge a reserved child's actual usage back to this budget.

        Releases the child's full reservation and adds only its
        actual model/tool calls (plus any delegations it spawned
        beyond the count it inherited). Settling an unknown or
        already-settled child raises, and unverified usage keeps the
        reservation — settle_code's strictness, for the same reason:
        a lost or double settle would leak or double-charge.
        """
        entry = next((e for e in self._child_leases if e[0] is child), None)
        if entry is None:
            raise ValueError("Child budget reservation is missing or already settled")
        if not (0 <= child.model_calls <= child.model_limit) or not (
            0 <= child.tool_calls <= child.tool_limit
        ):
            raise ValueError("Child budget usage is unverified; reservation retained")
        self._child_leases.remove(entry)
        self.reserved_model_calls -= child.model_limit
        self.reserved_tool_calls -= child.tool_limit
        self.model_calls += child.model_calls
        self.tool_calls += child.tool_calls
        self.delegations += max(0, child.delegations - entry[1])
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
