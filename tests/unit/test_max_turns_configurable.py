"""FF-R1 B3 (FF-G9) — the react loop's turn ceiling is configurable.

``MAX_REACT_ITERATIONS`` used to be a hard-coded module constant:
the task-context override was clamped to it, so no deployment could
ever run a longer (or shorter) loop. The ceiling now comes from
Settings — ``agent_max_turns`` (default 6, the historical constant)
with ``agent_max_turns_hard_cap`` as the configurable absolute cap a
task-context override can never exceed. Invalid overrides (0,
negatives, strings, bools) keep being ignored, exactly as before.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from forgeflow.runtime.react_executor import MAX_REACT_ITERATIONS, _resolve_max_iterations


def _task(context=None):
    return SimpleNamespace(context=dict(context or {}))


def _settings(max_turns=6, hard_cap=24):
    return SimpleNamespace(agent_max_turns=max_turns, agent_max_turns_hard_cap=hard_cap)


def test_default_ceiling_matches_the_historical_constant():
    assert _resolve_max_iterations(_task(), _settings()) == MAX_REACT_ITERATIONS == 6


def test_settings_value_drives_the_ceiling():
    assert _resolve_max_iterations(_task(), _settings(max_turns=3)) == 3
    assert _resolve_max_iterations(_task(), _settings(max_turns=12)) == 12


def test_task_override_below_the_settings_value_still_wins():
    assert _resolve_max_iterations(_task({"max_react_iterations": 2}), _settings()) == 2


def test_task_override_may_exceed_the_old_constant_up_to_the_hard_cap():
    # The whole point of FF-G9: 10 was impossible while the constant
    # clamped every override to 6.
    assert _resolve_max_iterations(_task({"max_react_iterations": 10}), _settings()) == 10


def test_task_override_is_clamped_by_the_configurable_hard_cap():
    assert _resolve_max_iterations(_task({"max_react_iterations": 100}), _settings()) == 24
    assert _resolve_max_iterations(_task({"max_react_iterations": 100}), _settings(hard_cap=8)) == 8


@pytest.mark.parametrize("bad", [0, -1, "3", True, 2.5, None])
def test_invalid_task_overrides_are_ignored(bad):
    task = _task({"max_react_iterations": bad})
    assert _resolve_max_iterations(task, _settings(max_turns=4)) == 4


def test_client_override_of_1000_is_clamped_to_the_hard_cap():
    # Dachuan's condition on the FF-G9 semantics (2026-11-23): a client
    # may ask for any ceiling it likes — what it gets is the hard cap.
    assert _resolve_max_iterations(_task({"max_react_iterations": 1000}), _settings()) == 24


def test_task_context_cannot_raise_the_hard_cap_itself():
    # The hard cap is server configuration only: lookalike keys in the
    # client-supplied task context must not move it (nor the base).
    task = _task(
        {
            "max_react_iterations": 1000,
            "agent_max_turns_hard_cap": 9999,
            "agent_max_turns": 500,
        }
    )
    assert _resolve_max_iterations(task, _settings()) == 24
    task = _task({"agent_max_turns_hard_cap": 1, "max_react_iterations": 3})
    assert _resolve_max_iterations(task, _settings()) == 3


def test_settings_model_validates_the_new_fields():
    from pydantic import ValidationError

    from forgeflow.config import Settings

    assert Settings().agent_max_turns == 6
    assert Settings().agent_max_turns_hard_cap == 24
    assert Settings(agent_max_turns=3).agent_max_turns == 3
    with pytest.raises(ValidationError):
        Settings(agent_max_turns=0)
    with pytest.raises(ValidationError):
        Settings(agent_max_turns_hard_cap=0)
