"""Delegation cap must be a returned result, not a raised error.

2026-10-30 delegation run 28b7563f hit the 24 model-call ceiling with
no aggregation: the old code raised BudgetExceeded when the delegation
cap (4) was reached, and the model retried the identical delegate call
until the model budget ran out — the same loop pattern the
resource/requirement gates had before drop-and-report. A cap is now
reported so the parent can aggregate and finish.
"""

import asyncio
from types import SimpleNamespace

from forgeflow.runtime.delegation import delegate, delegate_parallel
from forgeflow.runtime.execution_budget import ExecutionBudget, current_budget


def _ctx():
    parent = SimpleNamespace(delegation_role=None, allowed_tools=None)
    task = SimpleNamespace(context={"resources": []})
    return SimpleNamespace(
        runtime_context=parent,
        task_context=task,
        tenant_id="t",
        user_id="u",
        role="admin",
        run_id="p",
    )


def test_delegation_cap_returns_not_raises():
    ctx = _ctx()
    budget = ExecutionBudget()
    budget.delegations = 4
    token = current_budget.set(budget)
    try:
        result = asyncio.run(delegate({"role": "research", "instruction": "review"}, ctx))
    finally:
        current_budget.reset(token)
    assert result["ok"] is False
    assert "do not call agent.delegate again" in result["error"]


def test_near_model_limit_returns_not_starts_child():
    ctx = _ctx()
    budget = ExecutionBudget()
    budget.model_calls = 23
    token = current_budget.set(budget)
    try:
        result = asyncio.run(delegate({"role": "research", "instruction": "review"}, ctx))
    finally:
        current_budget.reset(token)
    assert result["ok"] is False
    assert "do not call agent.delegate again" in result["error"]
    assert budget.delegations == 0


def test_parallel_near_model_limit_skips_items():
    ctx = _ctx()
    budget = ExecutionBudget()
    budget.model_calls = 23
    token = current_budget.set(budget)
    try:
        result = asyncio.run(
            delegate_parallel(
                {"delegations": [{"role": "research", "instruction": "review"}]},
                ctx,
            )
        )
    finally:
        current_budget.reset(token)
    assert result["results"][0]["ok"] is False
    assert "nearly exhausted" in result["results"][0]["error"]


# --- regression: ONE cap computation for single + parallel -------------------- #


def test_cap_helper_is_the_single_computation():
    """The cap used to be computed twice with different conditions
    (batch-local OR budget in parallel; budget-only in single). The
    shared helper must agree for both views of the same state."""
    from forgeflow.runtime.delegation import (
        DELEGATION_CAP,
        _delegation_cap_reached,
        _model_budget_nearly_exhausted,
    )

    budget = ExecutionBudget()
    budget.delegations = DELEGATION_CAP - 1
    # Single path (no batch pending): one slot remains.
    assert _delegation_cap_reached(budget) is False
    # Parallel path with one brief already accepted in this batch:
    # the same state is at cap — total can never exceed the cap.
    assert _delegation_cap_reached(budget, pending=1) is True
    budget.delegations = DELEGATION_CAP
    assert _delegation_cap_reached(budget) is True
    assert _delegation_cap_reached(budget, pending=2) is True
    # The near-exhausted model-budget check is shared too.
    budget.model_calls = budget.model_limit - 3
    assert _model_budget_nearly_exhausted(budget) is False
    budget.model_calls = budget.model_limit - 2
    assert _model_budget_nearly_exhausted(budget) is True


def test_parallel_batch_counts_slots_already_used(monkeypatch):
    """With 3 of 4 slots used, a 2-brief batch runs exactly one brief
    and skips the other — the same total the single path enforces."""
    import forgeflow.runtime.delegation as delegation_mod

    ran: list[str] = []

    async def _fake_delegate(brief, ctx):
        ran.append(brief["instruction"])
        return {"ok": True, "summary": "done", "evidence": [], "delegations": []}

    monkeypatch.setattr(delegation_mod, "delegate", _fake_delegate)
    ctx = _ctx()
    budget = ExecutionBudget()
    budget.delegations = 3
    token = current_budget.set(budget)
    try:
        result = asyncio.run(
            delegate_parallel(
                {
                    "delegations": [
                        {"role": "research", "instruction": "one"},
                        {"role": "research", "instruction": "two"},
                    ]
                },
                ctx,
            )
        )
    finally:
        current_budget.reset(token)
    assert ran == ["one"]
    skipped = [r for r in result["results"] if "cap is 4" in str(r.get("error") or "")]
    assert len(skipped) == 1
    assert result["results"][1] in skipped


def test_child_iterations_helper_inherits_and_defaults():
    """Grandchild iteration ceiling: inherited from the parent when
    the parent carries a valid override, else the module default —
    never a stray literal at the call site."""
    from forgeflow.runtime.delegation import (
        CHILD_MAX_REACT_ITERATIONS,
        _child_max_react_iterations,
    )

    def ctx_with(context):
        parent = SimpleNamespace(delegation_role=None, allowed_tools=None)
        task = SimpleNamespace(context=context)
        return SimpleNamespace(
            runtime_context=parent, task_context=task,
            tenant_id="t", user_id="u", role="admin", run_id="p",
        )

    assert _child_max_react_iterations(ctx_with({})) == CHILD_MAX_REACT_ITERATIONS
    assert _child_max_react_iterations(ctx_with({"max_react_iterations": 2})) == 2
    assert _child_max_react_iterations(ctx_with({"max_react_iterations": "3"})) == CHILD_MAX_REACT_ITERATIONS
    assert _child_max_react_iterations(ctx_with({"max_react_iterations": True})) == CHILD_MAX_REACT_ITERATIONS
    assert _child_max_react_iterations(ctx_with({"max_react_iterations": 0})) == CHILD_MAX_REACT_ITERATIONS
