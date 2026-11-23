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


# --- FF-R1 B3b: child budgets are cut from the parent, then settled ------ #


def test_reserve_child_gives_the_child_its_own_budget():
    """Report §5: the child gets a DIFFERENT budget object; before
    settle, the parent's counters do not move — the child's share sits
    in the parent's reservations instead."""
    parent = ExecutionBudget()
    child = parent.reserve_child(model_cap=4, tool_cap=64)
    assert child is not None
    assert child is not parent
    assert child.model_limit == 4
    assert parent.model_calls == 0
    assert parent.tool_calls == 0
    assert parent.reserved_model_calls == 4


def test_settle_child_records_actual_usage_and_releases_the_rest():
    parent = ExecutionBudget()
    child = parent.reserve_child(model_cap=4, tool_cap=64)
    child.consume("model")
    child.consume("model")
    child.consume("tool")
    child.consume("tool")
    child.consume("tool")
    parent.settle_child(child)
    assert parent.model_calls == 2
    assert parent.tool_calls == 3
    # The unused part of the reservation is released, not charged.
    assert parent.reserved_model_calls == 0
    assert parent.reserved_tool_calls == 0


def test_settle_child_twice_is_rejected():
    import pytest

    parent = ExecutionBudget()
    child = parent.reserve_child(model_cap=4, tool_cap=64)
    parent.settle_child(child)
    with pytest.raises(ValueError):
        parent.settle_child(child)


def test_reserve_child_keeps_the_parent_floor():
    """A child that burns its whole share still leaves the parent its
    floor: 2 model calls, 2 tool calls, ~30 seconds (report §4.1)."""
    parent = ExecutionBudget()
    parent.model_calls = 20
    parent.tool_calls = 61
    child = parent.reserve_child(model_cap=4, tool_cap=64)
    assert child is not None
    assert child.model_limit == 2  # 24 - 20 - floor 2
    assert child.tool_limit == 1  # 64 - 61 - floor 2
    assert child.seconds_limit <= 600 - 30
    child.consume("model")
    child.consume("model")
    parent.settle_child(child)
    assert parent.model_calls == 22
    # The floor is really usable by the parent afterwards.
    parent.consume("model")
    parent.consume("model")


def test_reserve_child_refuses_when_only_the_floor_remains():
    parent = ExecutionBudget()
    parent.model_calls = 22  # only the 2-call floor is left
    assert parent.reserve_child(model_cap=4, tool_cap=64) is None
    assert parent.reserved_model_calls == 0


def test_parallel_split_gives_every_child_a_nonzero_share(monkeypatch):
    """Report §3.3/§4.3: 4-way parallel with the parent at 16/24 model
    calls — the old code stacked a 2-call reserve per child (8 total)
    and children could starve. The batch now splits what remains
    after ONE parent floor evenly: (24 - 16 - 2) // 4 = 1 each."""
    import forgeflow.runtime.delegation as delegation_mod

    shares: list[dict] = []

    async def _fake_delegate(brief, ctx):
        shares.append(dict(brief.get("_budget_share") or {}))
        return {"ok": True, "summary": "done", "evidence": [], "delegations": []}

    monkeypatch.setattr(delegation_mod, "delegate", _fake_delegate)
    ctx = _ctx()
    budget = ExecutionBudget()
    budget.model_calls = 16
    token = current_budget.set(budget)
    try:
        result = asyncio.run(
            delegate_parallel(
                {
                    "delegations": [
                        {"role": "research", "instruction": f"brief {i}"} for i in range(4)
                    ]
                },
                ctx,
            )
        )
    finally:
        current_budget.reset(token)
    assert result["ok_count"] == 4
    assert len(shares) == 4
    assert all(share.get("model_cap") == 1 for share in shares)
    # Shares + the single parent floor fit inside what remained (8).
    assert sum(share["model_cap"] for share in shares) + 2 <= 8


def test_child_token_cut_comes_from_the_parent_remaining():
    """Report §3.2/§4.2: the child's loop_token_budget is cut from
    the parent's remaining tokens (minus a floor for the parent's own
    aggregation), so children writing usage back can never push the
    parent past its own token budget."""
    from forgeflow.runtime.delegation import _child_loop_token_budget

    parent_task = SimpleNamespace(context={"llm_usage": []})
    assert _child_loop_token_budget(parent_task) == 250_000 - 25_000
    parent_task = SimpleNamespace(
        context={"llm_usage": [{"input_tokens": 150_000, "output_tokens": 50_000}]}
    )
    cut = _child_loop_token_budget(parent_task)
    assert cut == 250_000 - 200_000 - 25_000
    # Write-back bound: spent + cut + floor == the parent's budget.
    assert 200_000 + cut + 25_000 == 250_000


def test_delegate_skips_when_token_budget_nearly_exhausted():
    ctx = _ctx()
    ctx.task_context.context["llm_usage"] = [{"input_tokens": 200_000, "output_tokens": 40_000}]
    budget = ExecutionBudget()
    token = current_budget.set(budget)
    try:
        result = asyncio.run(delegate({"role": "research", "instruction": "review"}, ctx))
    finally:
        current_budget.reset(token)
    assert result["ok"] is False
    assert "token" in str(result["error"]).lower()
    assert budget.delegations == 0


def test_real_child_spends_its_own_budget_and_settles_to_the_parent(monkeypatch):
    """End-to-end through delegate() with a stubbed child executor:
    the child runs on a DIFFERENT budget object (cap 4), and when it
    finishes, its actual usage — and only that — lands on the parent,
    with the reservation fully released and its token usage written
    back under the cut it was given."""
    from forgeflow.runtime.orchestrator import RunRecord

    captured: dict = {}

    class _FakeExecutor:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, task, child, bus, run_id):
            budget = current_budget.get()
            captured["budget"] = budget
            captured["token_budget"] = task.context.get("loop_token_budget")
            budget.consume("model")
            budget.consume("model")
            budget.consume("tool")
            task.context.setdefault("llm_runtime", {})["final_answer"] = {"text": "done"}
            task.context["llm_usage"] = [{"input_tokens": 1000, "output_tokens": 500}]
            task.context["tool_invocations"] = [
                {"tool": "research.search", "status": "ok", "executed": True, "payload": {}}
            ]
            return [], []

    class _FakeBus:
        async def emit(self, *args, **kwargs):
            return None

    class _FakeStore:
        async def save(self, *args, **kwargs):
            return None

    async def _fake_scope(task, child):
        return None

    def _fake_register(**kwargs):
        return RunRecord(
            run_id=kwargs["run_id"],
            thread_id=kwargs["run_id"],
            tenant_id=kwargs.get("tenant_id"),
            agent_id=None,
            intent=kwargs.get("intent", ""),
            status="running",
            outcome="",
            steps=[],
            errors=[],
            created_at="2026-11-23T00:00:00+00:00",
            tool_invocations=[],
        )

    import forgeflow.runtime.events as events_mod
    import forgeflow.runtime.input_scope as scope_mod
    import forgeflow.runtime.orchestrator as orch_mod
    import forgeflow.runtime.react_executor as react_mod
    import forgeflow.workspace.store as ws_mod

    monkeypatch.setattr(events_mod, "get_event_bus", lambda: _FakeBus())
    monkeypatch.setattr(orch_mod, "register_running_run", _fake_register)
    monkeypatch.setattr(ws_mod, "get_workspace_store", lambda: _FakeStore())
    monkeypatch.setattr(scope_mod, "prepare_input_scope", _fake_scope)
    monkeypatch.setattr(react_mod, "ReactExecutor", _FakeExecutor)

    parent = SimpleNamespace(delegation_role=None, allowed_tools=None)
    task = SimpleNamespace(context={"resources": [], "llm_runtime": {}})
    ctx = SimpleNamespace(
        runtime_context=parent,
        task_context=task,
        tenant_id="t",
        user_id="u",
        role="admin",
        run_id="p",
    )
    budget = ExecutionBudget()
    token = current_budget.set(budget)
    try:
        result = asyncio.run(delegate({"role": "research", "instruction": "review"}, ctx))
    finally:
        current_budget.reset(token)
    assert result["ok"] is True
    assert captured["budget"] is not budget
    assert captured["budget"].model_limit == 4
    assert captured["token_budget"] == 225_000
    assert budget.model_calls == 2
    assert budget.tool_calls == 1
    assert budget.reserved_model_calls == 0
    assert budget.reserved_tool_calls == 0
    assert budget.delegations == 1
    assert task.context["llm_usage"][0]["input_tokens"] == 1000


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
            runtime_context=parent,
            task_context=task,
            tenant_id="t",
            user_id="u",
            role="admin",
            run_id="p",
        )

    assert _child_max_react_iterations(ctx_with({})) == CHILD_MAX_REACT_ITERATIONS
    assert _child_max_react_iterations(ctx_with({"max_react_iterations": 2})) == 2
    assert (
        _child_max_react_iterations(ctx_with({"max_react_iterations": "3"}))
        == CHILD_MAX_REACT_ITERATIONS
    )
    assert (
        _child_max_react_iterations(ctx_with({"max_react_iterations": True}))
        == CHILD_MAX_REACT_ITERATIONS
    )
    assert (
        _child_max_react_iterations(ctx_with({"max_react_iterations": 0}))
        == CHILD_MAX_REACT_ITERATIONS
    )
