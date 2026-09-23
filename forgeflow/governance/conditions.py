"""ABAC condition expressions for the PolicyEngine (INC2-17).

The original engine matched a policy's ``condition`` as a flat
``{key: value}`` equality map — ``str(context.get(key)) == str(expected)``.
This module keeps that exact behaviour and *adds* a small, auditable operator
set on top:

Per-attribute operators (opt-in — activated only when the expected side is a
mapping whose keys are **all** recognised operators)::

    eq         — equality            (same as the legacy bare value)
    ne         — inequality          {"role": {"ne": "viewer"}}
    in         — membership          {"role": {"in": ["manager", "admin"]}}
    gt / lt    — numeric compare     {"amount": {"gt": 1000}}
    contains   — substring / member  {"path": {"contains": "admin"}}

Logical combinators (a reserved key whose value is a *list* of sub-conditions)::

    all        — every sub-condition holds
    any        — at least one sub-condition holds

Backward compatibility is a hard constraint (``tests/unit/test_policy_engine.py``
must not regress). A plain ``{key: value}`` pair always evaluates exactly as
before; a key literally named ``all``/``any`` whose value is **not** a list is
treated as an ordinary attribute and falls through to the legacy matcher.

Everything fails closed: a malformed condition denies (returns ``False``)
rather than silently matching.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "CLAUSE_OPERATORS",
    "LOGICAL_OPERATORS",
    "OPERATORS",
    "UnknownOperatorError",
    "evaluate_condition",
    "evaluate_operator",
]

#: Per-attribute operators.
CLAUSE_OPERATORS: frozenset[str] = frozenset({"eq", "ne", "in", "gt", "lt", "contains"})
#: Logical combinators over a list of sub-conditions.
LOGICAL_OPERATORS: frozenset[str] = frozenset({"all", "any"})
#: Everything this module understands.
OPERATORS: frozenset[str] = CLAUSE_OPERATORS | LOGICAL_OPERATORS


class UnknownOperatorError(ValueError):
    """Raised when :func:`evaluate_operator` is handed an unknown operator."""


def _equal(actual: Any, expected: Any) -> bool:
    """Legacy equality: a stringified comparison (preserves old semantics)."""
    return str(actual) == str(expected)


def _as_number(value: Any) -> float | None:
    """Best-effort numeric coercion; ``None`` when the value is not a number."""
    if value is None or isinstance(value, bool):
        # Bools are not meaningful operands for gt/lt budgeting rules.
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _op_contains(actual: Any, expected: Any) -> bool:
    """Substring (str) or membership (collection) test."""
    if actual is None:
        return False
    if isinstance(actual, str):
        return str(expected) in actual
    try:
        return expected in actual  # type: ignore[operator]
    except TypeError:
        return str(expected) in str(actual)


def evaluate_operator(operator: str, actual: Any, expected: Any, context: dict[str, Any]) -> bool:
    """Evaluate a single ``operator`` of ``actual`` against ``expected``.

    ``context`` is accepted for future context-aware operators and to keep the
    call signature stable; the current operators only need the two operands.
    """
    op = (operator or "").strip().lower()
    if op == "eq":
        return _equal(actual, expected)
    if op == "ne":
        return not _equal(actual, expected)
    if op == "in":
        if not isinstance(expected, (list, tuple, set, frozenset)):
            return False
        return any(_equal(actual, candidate) for candidate in expected)
    if op == "gt":
        left, right = _as_number(actual), _as_number(expected)
        return left is not None and right is not None and left > right
    if op == "lt":
        left, right = _as_number(actual), _as_number(expected)
        return left is not None and right is not None and left < right
    if op == "contains":
        return _op_contains(actual, expected)
    raise UnknownOperatorError(f"unknown operator '{operator}'")


def _is_operator_map(expected: Any) -> bool:
    """True when ``expected`` is a non-empty mapping using only known operators."""
    if not isinstance(expected, dict) or not expected:
        return False
    return all(isinstance(key, str) and key.strip().lower() in OPERATORS for key in expected)


def _evaluate_clause(key: str, expected: Any, context: dict[str, Any]) -> bool:
    """Match one ``key`` of the condition against ``context``."""
    actual = context.get(key)
    if _is_operator_map(expected):
        for raw_op, operand in expected.items():
            op = raw_op.strip().lower()
            if op in LOGICAL_OPERATORS:
                # Combinators belong at the condition level, not on one attribute.
                return False
            if not evaluate_operator(op, actual, operand, context):
                return False
        return True
    # Legacy path — unchanged behaviour.
    return _equal(actual, expected)


def evaluate_condition(condition: dict[str, Any] | None, context: dict[str, Any] | None) -> bool:
    """Evaluate an ABAC ``condition`` against a ``context``.

    An empty/absent condition matches everything (as before). Every clause must
    hold (implicit AND); ``all``/``any`` clauses combine sub-conditions.
    """
    if not condition:
        return True
    if not isinstance(condition, dict):
        return False

    ctx = context if isinstance(context, dict) else {}

    for key, expected in condition.items():
        if key in LOGICAL_OPERATORS and isinstance(expected, (list, tuple, set, frozenset)):
            sub_conditions = list(expected)
            if key == "all":
                if not all(evaluate_condition(sub, ctx) for sub in sub_conditions):
                    return False
            else:  # any
                if not any(evaluate_condition(sub, ctx) for sub in sub_conditions):
                    return False
            continue
        if not _evaluate_clause(key, expected, ctx):
            return False
    return True
