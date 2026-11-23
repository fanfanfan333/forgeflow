"""FF-R1 B2 (FF-G4) — ``Budget.from_settings`` is fail-closed.

Pre-fix, ``from_settings`` built every dimension with ``value or
DEFAULT``: an operator (or a broken config source) setting a ceiling
to ``0`` silently got the default ceiling instead — the exact
fail-open shape the rest of B2 removes. A dimension explicitly set to
``0`` or a negative number is a configuration error and must raise;
a dimension that is absent / ``None`` still falls back to its default.
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
    _with_settings(monkeypatch, agent_loop_max_steps=0)
    with pytest.raises(BudgetConfigError):
        Budget.from_settings()


def test_zero_wall_time_raises(monkeypatch) -> None:
    _with_settings(monkeypatch, agent_loop_wall_time_seconds=0)
    with pytest.raises(BudgetConfigError):
        Budget.from_settings()


def test_zero_max_tokens_raises(monkeypatch) -> None:
    _with_settings(monkeypatch, agent_loop_max_tokens=0)
    with pytest.raises(BudgetConfigError):
        Budget.from_settings()


def test_zero_max_replans_raises(monkeypatch) -> None:
    _with_settings(monkeypatch, agent_loop_max_replans=0)
    with pytest.raises(BudgetConfigError):
        Budget.from_settings()


def test_negative_value_raises(monkeypatch) -> None:
    _with_settings(monkeypatch, agent_loop_max_steps=-1)
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
        agent_loop_max_steps=7,
        agent_loop_max_replans=2,
        agent_loop_wall_time_seconds=42.5,
        agent_loop_max_tokens=1234,
    )
    budget = Budget.from_settings()
    assert budget.max_steps == 7
    assert budget.max_replans == 2
    assert budget.wall_time_seconds == 42.5
    assert budget.max_tokens == 1234
