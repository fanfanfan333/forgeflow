"""Loop breaker — the Agent Loop's token / wall-clock budget (review finding #10).

Two layers are pinned here:

* the **policy** (``check_budget``) as a pure function, and
* the **wiring**, so the breaker cannot silently rot back into a module nobody
  calls — the failure mode this project has already hit twice
  (``context_builder``, ``submit_advice_for_approval``).

The last test is the important one: under the shipped defaults a failing run must
still behave *exactly* as before (2 replans, no breaker event), so the new ceiling
only ever fires when an operator actually tightens the budget.
"""

from __future__ import annotations

import pytest

from forgeflow.config import get_settings
from forgeflow.repositories.memory.policy_repo import MemoryPolicyRepository
from forgeflow.runtime.orchestrator import RequestContext, TaskCreate, run_task
from forgeflow.validation.loop_breaker import (
    DIMENSIONS,
    LoopBreaker,
    LoopBudget,
    check_budget,
)

_BUDGET = LoopBudget(max_tokens=1000, max_seconds=60.0)


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, run_id: str, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.events]


def _failing_task(**context: object) -> TaskCreate:
    return TaskCreate(intent="分析华东销售数据", context={"simulate_failure": True, **context})


# --------------------------------------------------------------------------- #
# Policy — check_budget (pure)                                                 #
# --------------------------------------------------------------------------- #

def test_inside_budget_returns_none():
    assert check_budget(_BUDGET, tokens=999, seconds=59.9) is None


def test_tokens_dimension_is_reported():
    breach = check_budget(_BUDGET, tokens=1000, seconds=1.0)
    assert breach is not None
    assert breach.dimension == "tokens"
    assert breach.used == 1000
    assert breach.limit == 1000


def test_seconds_dimension_is_reported():
    breach = check_budget(_BUDGET, tokens=0, seconds=60.0)
    assert breach is not None
    assert breach.dimension == "seconds"


def test_tokens_win_when_both_dimensions_breach():
    """Deterministic ordering — DIMENSIONS is the tie-break, not dict order."""
    breach = check_budget(_BUDGET, tokens=5000, seconds=999.0)
    assert breach is not None
    assert breach.dimension == DIMENSIONS[0] == "tokens"


def test_negative_limit_disables_that_dimension():
    """An operator switches a ceiling off (only) with a strictly negative limit."""
    budget = LoopBudget(max_tokens=-1, max_seconds=10.0)
    assert check_budget(budget, tokens=10**9, seconds=1.0) is None


def test_zero_token_limit_trips_immediately_even_at_zero_usage():
    """``0`` is a *zero budget*, not "off" — fail-closed: it trips at ``used == 0``.

    Reading ``0`` as "disable" would silently lift the budget protection, which
    is why the implementation keeps ``0`` as a tripping ceiling. This pins the
    real semantics so the docstring can never drift back to "0 disables".
    """
    budget = LoopBudget(max_tokens=0, max_seconds=10.0)
    breach = check_budget(budget, tokens=0, seconds=1.0)
    assert breach is not None
    assert breach.dimension == "tokens"
    assert breach.used == 0
    assert breach.limit == 0


def test_zero_seconds_limit_trips_immediately_on_every_dimension():
    """The zero-budget rule holds for *every* dimension, not just tokens."""
    budget = LoopBudget(max_tokens=1000, max_seconds=0.0)
    breach = check_budget(budget, tokens=10, seconds=0.0)
    assert breach is not None
    assert breach.dimension == "seconds"


def test_breach_message_names_the_dimension_and_both_numbers():
    breach = check_budget(_BUDGET, tokens=1000, seconds=0.0)
    assert breach is not None
    assert "tokens" in breach.message
    assert "1000" in breach.message
    assert "HITL" in breach.message


def test_breach_is_json_serialisable():
    breach = check_budget(_BUDGET, tokens=1000, seconds=0.0)
    assert breach is not None
    assert breach.to_dict() == {
        "dimension": "tokens",
        "used": 1000.0,
        "limit": 1000.0,
        "message": breach.message,
    }


# --------------------------------------------------------------------------- #
# State — LoopBreaker                                                          #
# --------------------------------------------------------------------------- #

def test_breaker_is_clean_and_counts_its_observations():
    breaker = LoopBreaker(_BUDGET)
    assert breaker.tripped is False
    assert breaker.observe(tokens=10, seconds=1.0) is None
    assert breaker.observe(tokens=20, seconds=2.0) is None
    assert breaker.observations == 2
    # "never consulted" must stay distinguishable from "consulted, all clear".
    assert breaker.to_dict()["observations"] == 2
    assert breaker.to_dict()["tripped"] is False
    assert breaker.to_dict()["breaches"] == []


def test_breaker_records_each_dimension_only_once():
    breaker = LoopBreaker(_BUDGET)
    assert breaker.observe(tokens=2000, seconds=1.0) is not None
    assert breaker.observe(tokens=3000, seconds=1.0) is not None
    assert breaker.observe(tokens=0, seconds=999.0) is not None
    assert breaker.observe(tokens=0, seconds=999.0) is not None

    assert [b.dimension for b in breaker.breaches] == ["tokens", "seconds"]
    assert breaker.observations == 4  # asks are counted, breaches are deduped


def test_budget_from_settings_reads_the_configured_ceilings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "max_run_tokens", 123)
    monkeypatch.setattr(settings, "max_run_seconds", 4.5)

    budget = LoopBudget.from_settings()
    assert budget.max_tokens == 123
    assert budget.max_seconds == pytest.approx(4.5)
    assert budget.to_dict() == {"max_tokens": 123, "max_seconds": 4.5}


# --------------------------------------------------------------------------- #
# Wiring — run_task consults the breaker                                       #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_token_budget_vetoes_the_retry_and_escalates_to_hitl(monkeypatch):
    """The count ceiling authorises a retry; the budget ceiling cancels it."""
    monkeypatch.setattr(get_settings(), "max_run_tokens", 1000)
    bus = _RecordingBus()
    pol_repo = MemoryPolicyRepository()
    task = _failing_task(
        llm_usage=[{"agent": "supervisor", "input_tokens": 800, "output_tokens": 400}]
    )
    ctx = RequestContext(tenant_id="t-loop-tokens", user_id="admin-1", role="admin")

    handle = await run_task(task, ctx, bus=bus, policy_repo=pol_repo)

    assert handle.status == "failed"
    assert handle.detail["loop"]["tripped"] is True
    assert handle.detail["loop"]["breaches"][0]["dimension"] == "tokens"
    # The authorised retry never ran, so no replan was consumed.
    assert handle.detail["replans"] == 0
    assert any("预算熔断" in err for err in handle.detail["errors"])
    assert "run.loop.breaker" in bus.kinds()
    # ...and a human now owns the run.
    approvals = await pol_repo.list_approvals("t-loop-tokens", status="pending")
    assert len(approvals) == 1
    assert "replan-exhausted" in approvals[0].requested_action


@pytest.mark.asyncio
async def test_wall_clock_budget_vetoes_the_retry(monkeypatch):
    """A near-zero wall-clock ceiling trips on the first cancelled retry."""
    monkeypatch.setattr(get_settings(), "max_run_seconds", 1e-9)
    bus = _RecordingBus()
    pol_repo = MemoryPolicyRepository()
    ctx = RequestContext(tenant_id="t-loop-clock", user_id="admin-1", role="admin")

    handle = await run_task(_failing_task(), ctx, bus=bus, policy_repo=pol_repo)

    assert handle.status == "failed"
    assert handle.detail["loop"]["breaches"][0]["dimension"] == "seconds"
    assert handle.detail["replans"] == 0


@pytest.mark.asyncio
async def test_default_budget_leaves_the_existing_behaviour_untouched():
    """No regression: under shipped defaults a failing run still replans twice."""
    bus = _RecordingBus()
    ctx = RequestContext(tenant_id="t-loop-default", user_id="admin-1", role="admin")

    handle = await run_task(_failing_task(), ctx, bus=bus)

    assert handle.status == "failed"
    assert handle.detail["replans"] == 2
    assert handle.detail["loop"]["tripped"] is False
    assert handle.detail["loop"]["observations"] == 2
    assert "run.loop.breaker" not in bus.kinds()
    assert sum(1 for kind in bus.kinds() if kind == "replan") == 3
