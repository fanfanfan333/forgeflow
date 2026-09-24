"""INC9 B3 — memory type dimension (default derivation / validation / ladder).

Covers docs/sop/12-INC9-DESIGN.md §10 (unit, pure functions).
"""

from __future__ import annotations

import pytest

from forgeflow.experience.memory_types import (
    MEMORY_TYPES,
    TYPE_LADDER,
    MemoryType,
    default_type_for_scope,
    is_valid_type,
    next_type,
)


def test_four_types_are_declared():
    assert MEMORY_TYPES == ("working", "episodic", "semantic", "procedural")
    assert [t.value for t in MemoryType] == list(MEMORY_TYPES)


def test_ladder_matches_types():
    assert TYPE_LADDER == MEMORY_TYPES


@pytest.mark.parametrize(
    "scope,expected",
    [
        ("user", "working"),
        ("team", "working"),
        ("episodic", "episodic"),
        ("semantic", "semantic"),
        ("org", "semantic"),
    ],
)
def test_default_type_for_scope(scope, expected):
    assert default_type_for_scope(scope) == expected


def test_unknown_scope_derives_working():
    assert default_type_for_scope("nonsense") == "working"


def test_is_valid_type():
    assert is_valid_type("semantic") is True
    assert is_valid_type("bogus") is False


def test_next_type_promote_advances_one_rung():
    assert next_type("working", "promote") == "episodic"
    assert next_type("episodic", "promote") == "semantic"
    assert next_type("semantic", "promote") == "procedural"


def test_next_type_promote_is_capped_at_procedural():
    assert next_type("procedural", "promote") == "procedural"


def test_next_type_demote_steps_down():
    assert next_type("semantic", "demote") == "episodic"
    assert next_type("working", "demote") == "working"


def test_next_type_unknown_event_is_noop():
    assert next_type("semantic", "whatever") == "semantic"


def test_next_type_normalises_invalid_current():
    assert next_type("bogus", "promote") == "episodic"
    assert next_type("", "demote") == "working"
