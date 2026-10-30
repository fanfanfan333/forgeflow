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
