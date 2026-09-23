"""INC2-05 — three-level budgets, the 80%/100% thresholds and degrade actions."""

from __future__ import annotations

import pytest

from forgeflow.cost.budget_service import BudgetService
from forgeflow.cost.degrade import (
    CONTEXT_BUDGET_MULTIPLIER,
    build_degrade_state,
    clear_degrade_callbacks,
    trigger_degrade,
)
from forgeflow.cost.models import DEFAULT_EXCEED_ACTIONS, WARN_ACTIONS
from forgeflow.repositories.factory import reset_repositories
from forgeflow.repositories.memory import clear_cost_budget_store

_TENANT = "tenant-degrade"


@pytest.fixture(autouse=True)
def _reset():
    reset_repositories()
    clear_cost_budget_store()
    yield
    clear_cost_budget_store()
    reset_repositories()


@pytest.fixture(autouse=True)
def _isolate_cost_budgets(pg_purge):
    """Clear the shared ``cost_budgets`` table around each case (PG profile).

    ``evaluate_budget`` reads the *live* repository, so with residual rows from
    an earlier test the "no budget ⇒ nothing to enforce" case finds a stale budget
    (``spent=999 / limit=50``) and degrades instead of returning ``scope='none'``.
    A no-op under the memory profile, where ``clear_cost_budget_store`` suffices.
    """
    pg_purge("cost_budgets")
    yield
    pg_purge("cost_budgets")



def _service() -> BudgetService:
    return BudgetService()


class TestThresholds:
    @pytest.mark.asyncio
    async def test_below_warn_ratio_is_ok(self):
        service = _service()
        await service.set_budget(_TENANT, "tenant", 100.0, warn_ratio=0.80)

        decision = await service.evaluate_budget(_TENANT, 79.0)

        assert decision.level == "ok"
        assert decision.actions == ()
        assert decision.has_budget is True
        assert decision.notify_only is True

    @pytest.mark.asyncio
    async def test_eighty_percent_is_warn(self):
        service = _service()
        await service.set_budget(_TENANT, "tenant", 100.0, warn_ratio=0.80)

        decision = await service.evaluate_budget(_TENANT, 80.0)

        assert decision.level == "warn"
        assert decision.ratio == pytest.approx(0.80)
        # A warn records + notifies but must NOT degrade.
        assert decision.actions == tuple(WARN_ACTIONS)
        assert decision.notify_only is True

    @pytest.mark.asyncio
    async def test_one_hundred_percent_is_exceeded(self):
        service = _service()
        await service.set_budget(_TENANT, "tenant", 100.0, warn_ratio=0.80)

        decision = await service.evaluate_budget(_TENANT, 100.0)

        assert decision.level == "exceeded"
        assert decision.ratio == pytest.approx(1.0)
        assert decision.notify_only is False

    @pytest.mark.asyncio
    async def test_exceeded_runs_all_actions_in_order(self):
        service = _service()
        await service.set_budget(_TENANT, "tenant", 50.0, warn_ratio=0.80)

        decision = await service.evaluate_budget(_TENANT, 75.0)

        assert decision.level == "exceeded"
        assert decision.actions == tuple(DEFAULT_EXCEED_ACTIONS)
        assert list(decision.actions) == [
            "swap_model",
            "trim_context",
            "pause_noncritical",
        ]
        assert decision.remaining == 0.0

    @pytest.mark.asyncio
    async def test_no_budget_means_nothing_to_enforce(self):
        decision = await _service().evaluate_budget("tenant-with-no-budget", 999.0)

        assert decision.has_budget is False
        assert decision.level == "ok"
        assert decision.scope == "none"


class TestDegradeActions:
    @pytest.mark.asyncio
    async def test_exceeded_decision_maps_to_degrade_state(self):
        service = _service()
        await service.set_budget(_TENANT, "tenant", 100.0)
        decision = await service.evaluate_budget(_TENANT, 150.0)

        state = service.degrade_state(decision)

        assert state.use_weak_model is True
        assert state.pause_noncritical is True
        assert state.context_budget_multiplier == CONTEXT_BUDGET_MULTIPLIER
        assert state.is_degraded is True

    @pytest.mark.asyncio
    async def test_warn_decision_never_degrades(self):
        service = _service()
        await service.set_budget(_TENANT, "tenant", 100.0)
        decision = await service.evaluate_budget(_TENANT, 85.0)

        state = service.degrade_state(decision)

        assert state.use_weak_model is False
        assert state.pause_noncritical is False
        assert state.context_budget_multiplier == 1.0
        assert state.is_degraded is False
        assert state.notified is True

    def test_pause_noncritical_refuses_only_non_core_workflows(self):
        state = build_degrade_state(["pause_noncritical"])

        assert state.denies_new_run("finance_recon") is True
        assert state.denies_new_run("support_ops") is True
        # Core paths keep running.
        assert state.denies_new_run("sales_ops") is False
        assert state.denies_new_run("agentflow_task") is False

    def test_trim_context_halves_the_budget(self):
        state = build_degrade_state(["trim_context"])

        assert state.effective_context_budget(2000) == 1000

    def test_unknown_action_is_ignored_not_raised(self):
        state = build_degrade_state(["swap_model", "not-a-real-action"])

        assert state.actions == ("swap_model",)
        assert state.use_weak_model is True


class TestThreeLevels:
    @pytest.mark.asyncio
    async def test_strictest_budget_binds(self):
        service = _service()
        await service.set_budget(_TENANT, "tenant", 1_000.0)
        await service.set_budget(_TENANT, "team", 100.0, scope_id="team-a")
        await service.set_budget(_TENANT, "task", 200.0, scope_id="task-1")

        decision = await service.evaluate_budget(
            _TENANT, 90.0, team_id="team-a", task_id="task-1"
        )

        assert decision.scope == "team"
        assert decision.limit == pytest.approx(100.0)
        assert decision.level == "warn"

    @pytest.mark.asyncio
    async def test_levels_are_only_consulted_when_ids_are_given(self):
        service = _service()
        await service.set_budget(_TENANT, "tenant", 100.0)
        await service.set_budget(_TENANT, "team", 10.0, scope_id="team-b")

        # No team_id ⇒ the stricter team budget must not leak in.
        decision = await service.evaluate_budget(_TENANT, 50.0)

        assert decision.scope == "tenant"
        assert decision.limit == pytest.approx(100.0)


class TestDegradeCallbacks:
    def test_trigger_degrade_notifies_listeners(self):
        seen: list[tuple[str, tuple[str, ...]]] = []
        clear_degrade_callbacks()

        from forgeflow.cost.degrade import register_degrade_callback

        register_degrade_callback(lambda tier, actions, ctx: seen.append((tier, actions)))
        try:
            trigger_degrade("edge", ["pause_noncritical"], reason="test")
        finally:
            clear_degrade_callbacks()

        assert seen == [("edge", ("pause_noncritical",))]

    def test_a_failing_callback_does_not_break_the_caller(self):
        clear_degrade_callbacks()

        from forgeflow.cost.degrade import register_degrade_callback

        def _boom(tier, actions, ctx):
            raise RuntimeError("listener exploded")

        register_degrade_callback(_boom)
        try:
            state = trigger_degrade("critical", ["swap_model"])
        finally:
            clear_degrade_callbacks()

        assert state.use_weak_model is True
