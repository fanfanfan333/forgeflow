"""FF-R1 B2 (FF-G4) — ``Budget.from_settings`` is fail-closed.

Pre-fix, ``from_settings`` built every dimension with ``value or
DEFAULT``: an operator (or a broken config source) setting a ceiling
to ``0`` silently got the default ceiling instead — the exact
fail-open shape the rest of B2 removes. A dimension explicitly set to
``0`` or a negative number is a configuration error and must raise;
a dimension that is absent / ``None`` still falls back to its default.

FF-R1 B5 note: the settings keys this suite passes were renamed
``agent_loop_*`` → ``budget_*`` when the agent loop (FF-G23) was
removed; the assertions are unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import forgeflow.config as config_module
from forgeflow.agent.budget import (
    DEFAULT_MAX_STEPS,
    DEFAULT_MAX_TOKENS,
    DEFAULT_WALL_TIME_SECONDS,
    Budget,
    BudgetConfigError,
)


def _with_settings(monkeypatch, **fields) -> None:
    monkeypatch.setattr(config_module, "get_settings", lambda: SimpleNamespace(**fields))


def test_zero_max_steps_raises(monkeypatch) -> None:
    _with_settings(monkeypatch, budget_max_steps=0)
    with pytest.raises(BudgetConfigError):
        Budget.from_settings()


def test_zero_wall_time_raises(monkeypatch) -> None:
    _with_settings(monkeypatch, budget_wall_time_seconds=0)
    with pytest.raises(BudgetConfigError):
        Budget.from_settings()


def test_zero_max_tokens_raises(monkeypatch) -> None:
    _with_settings(monkeypatch, budget_max_tokens=0)
    with pytest.raises(BudgetConfigError):
        Budget.from_settings()


def test_zero_max_replans_raises(monkeypatch) -> None:
    _with_settings(monkeypatch, budget_max_replans=0)
    with pytest.raises(BudgetConfigError):
        Budget.from_settings()


def test_negative_value_raises(monkeypatch) -> None:
    _with_settings(monkeypatch, budget_max_steps=-1)
    with pytest.raises(BudgetConfigError):
        Budget.from_settings()


def test_budget_config_error_is_a_value_error() -> None:
    assert issubclass(BudgetConfigError, ValueError)


def test_missing_fields_still_default(monkeypatch) -> None:
    _with_settings(monkeypatch)
    budget = Budget.from_settings()
    assert budget.max_steps == DEFAULT_MAX_STEPS
    assert budget.max_tokens == DEFAULT_MAX_TOKENS
    assert budget.wall_time_seconds == DEFAULT_WALL_TIME_SECONDS


def test_positive_values_pass_through(monkeypatch) -> None:
    _with_settings(
        monkeypatch,
        budget_max_steps=7,
        budget_max_replans=2,
        budget_wall_time_seconds=42.5,
        budget_max_tokens=1234,
    )
    budget = Budget.from_settings()
    assert budget.max_steps == 7
    assert budget.max_replans == 2
    assert budget.wall_time_seconds == 42.5
    assert budget.max_tokens == 1234


# --------------------------------------------------------------------------- #
# Supplement （大川裁定 on PR #10): a directly-passed 0 budget and an            #
# exhausted budget both fail closed at check time                              #
# --------------------------------------------------------------------------- #
def test_direct_zero_budget_trips_immediately() -> None:
    """``Budget(max_*=0)`` is a zero budget: it trips at zero usage.

    Constructing ``Budget`` directly (bypassing ``from_settings``) must
    not turn 0 into "no limit" — ``check_budget`` reports a breach for
    the zero dimension immediately.
    """
    from forgeflow.agent.budget import Budget, check_budget

    for dimension, kwargs in (
        ("steps", {"max_steps": 0}),
        ("replans", {"max_replans": 0}),
        ("seconds", {"wall_time_seconds": 0}),
        ("tokens", {"max_tokens": 0}),
    ):
        breach = check_budget(Budget(**kwargs))
        assert breach is not None, f"{dimension}=0 must trip immediately"
        assert breach.dimension == dimension


def test_exhausted_budget_fails_closed() -> None:
    """Usage at the ceiling is a breach; usage below it is not."""
    from forgeflow.agent.budget import Budget, check_budget

    budget = Budget(max_steps=2, max_replans=1, wall_time_seconds=10.0, max_tokens=100)
    assert check_budget(budget, steps=1, replans=0, seconds=9.9, tokens=99) is None
    assert check_budget(budget, steps=2) is not None
    assert check_budget(budget, replans=1) is not None
    assert check_budget(budget, seconds=10.0) is not None
    assert check_budget(budget, tokens=100) is not None
    assert check_budget(budget, tokens=101) is not None
