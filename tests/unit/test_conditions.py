"""INC2-17 — ABAC condition operators, with backward-compatibility guards.

The hard constraint: a plain ``{key: value}`` pair must evaluate exactly as it
did before (``tests/unit/test_policy_engine.py`` must not regress). The new
operators (``in`` / ``ne`` / ``gt`` / ``lt`` / ``contains`` + ``all`` / ``any``)
may only *add* behaviour.
"""

from __future__ import annotations

import pytest

from forgeflow.governance.conditions import (
    OPERATORS,
    UnknownOperatorError,
    evaluate_condition,
    evaluate_operator,
)
from forgeflow.governance.models import PolicyRecord
from forgeflow.governance.policy_engine import PolicyEngine, _condition_matches
from forgeflow.repositories.memory.policy_repo import MemoryPolicyRepository


# --------------------------------------------------------------------------- #
# Legacy semantics — must be byte-for-byte identical                           #
# --------------------------------------------------------------------------- #

class TestLegacyEqualityPreserved:
    def test_bare_equality_matches(self):
        assert evaluate_condition({"role": "manager"}, {"role": "manager"}) is True

    def test_bare_mismatch_denies(self):
        assert evaluate_condition({"role": "manager"}, {"role": "viewer"}) is False

    def test_string_coercion_is_preserved(self):
        # Old rule was str(context[key]) == str(expected).
        assert evaluate_condition({"stage": 2}, {"stage": "2"}) is True

    def test_empty_condition_matches_everything(self):
        assert evaluate_condition({}, {"role": "viewer"}) is True
        assert evaluate_condition(None, {"role": "viewer"}) is True

    def test_multiple_bare_pairs_are_anded(self):
        cond = {"role": "manager", "env": "prod"}
        assert evaluate_condition(cond, {"role": "manager", "env": "prod"}) is True
        assert evaluate_condition(cond, {"role": "manager", "env": "dev"}) is False

    def test_missing_key_does_not_match(self):
        assert evaluate_condition({"amount": 100}, {"role": "manager"}) is False

    def test_condition_matches_wrapper_delegates(self):
        assert _condition_matches({"role": "manager"}, {"role": "manager"}) is True
        assert _condition_matches({"role": "manager"}, {"role": "viewer"}) is False


# --------------------------------------------------------------------------- #
# New operators                                                                #
# --------------------------------------------------------------------------- #

class TestOperators:
    def test_ne(self):
        assert evaluate_condition({"role": {"ne": "viewer"}}, {"role": "manager"}) is True
        assert evaluate_condition({"role": {"ne": "viewer"}}, {"role": "viewer"}) is False

    def test_in(self):
        cond = {"role": {"in": ["manager", "admin"]}}
        assert evaluate_condition(cond, {"role": "admin"}) is True
        assert evaluate_condition(cond, {"role": "viewer"}) is False

    def test_in_requires_a_collection(self):
        # A non-collection operand fails closed rather than raising.
        assert evaluate_condition({"role": {"in": "manager"}}, {"role": "manager"}) is False

    def test_gt_lt(self):
        assert evaluate_condition({"amount": {"gt": 1000}}, {"amount": 1500}) is True
        assert evaluate_condition({"amount": {"gt": 1000}}, {"amount": 1000}) is False
        assert evaluate_condition({"amount": {"lt": 1000}}, {"amount": 999}) is True
        assert evaluate_condition({"amount": {"lt": 1000}}, {"amount": 1000}) is False

    def test_gt_on_non_numeric_fails_closed(self):
        assert evaluate_condition({"amount": {"gt": 10}}, {"amount": "many"}) is False

    def test_contains_substring(self):
        assert evaluate_condition({"path": {"contains": "admin"}}, {"path": "/api/admin/x"}) is True
        assert evaluate_condition({"path": {"contains": "admin"}}, {"path": "/api/user"}) is False

    def test_contains_membership(self):
        assert evaluate_condition({"tags": {"contains": "vip"}}, {"tags": ["vip", "eu"]}) is True
        assert evaluate_condition({"tags": {"contains": "vip"}}, {"tags": ["eu"]}) is False

    def test_explicit_eq(self):
        assert evaluate_condition({"role": {"eq": "manager"}}, {"role": "manager"}) is True

    def test_multiple_operators_are_anded(self):
        cond = {"amount": {"gt": 100, "lt": 1000}}
        assert evaluate_condition(cond, {"amount": 500}) is True
        assert evaluate_condition(cond, {"amount": 5000}) is False


class TestLogicalCombinators:
    def test_all(self):
        cond = {"all": [{"role": "manager"}, {"amount": {"gt": 10}}]}
        assert evaluate_condition(cond, {"role": "manager", "amount": 50}) is True
        assert evaluate_condition(cond, {"role": "manager", "amount": 5}) is False

    def test_any(self):
        cond = {"any": [{"role": "admin"}, {"role": "manager"}]}
        assert evaluate_condition(cond, {"role": "manager"}) is True
        assert evaluate_condition(cond, {"role": "viewer"}) is False

    def test_key_literally_named_all_with_non_list_is_legacy(self):
        # A non-list value must fall through to the legacy equality matcher,
        # so an attribute actually called "all" keeps working.
        assert evaluate_condition({"all": "x"}, {"all": "x"}) is True
        assert evaluate_condition({"all": "x"}, {"all": "y"}) is False

    def test_nested_combinators(self):
        cond = {"any": [{"all": [{"role": "admin"}, {"env": "prod"}]}, {"role": "owner"}]}
        assert evaluate_condition(cond, {"role": "admin", "env": "prod"}) is True
        assert evaluate_condition(cond, {"role": "owner"}) is True
        assert evaluate_condition(cond, {"role": "admin", "env": "dev"}) is False


class TestMalformedFailsClosed:
    def test_unknown_operator_name_in_map_is_legacy(self):
        # {"role": {"foo": "bar"}} is not a recognised operator map, so it is
        # compared as a literal mapping (legacy) → no match for a plain role.
        assert evaluate_condition({"role": {"foo": "bar"}}, {"role": "manager"}) is False

    def test_evaluate_operator_rejects_unknown(self):
        with pytest.raises(UnknownOperatorError):
            evaluate_operator("frobnicate", 1, 2, {})

    def test_operators_constant(self):
        assert {"in", "ne", "gt", "lt", "contains", "all", "any"} <= OPERATORS


# --------------------------------------------------------------------------- #
# Engine integration — the operators reach the real decision path              #
# --------------------------------------------------------------------------- #

def _tenant() -> str:
    import uuid

    return f"t-cond-{uuid.uuid4().hex[:8]}"


async def test_engine_denies_on_operator_condition():
    tenant = _tenant()
    repo = MemoryPolicyRepository()
    await repo.save_policy(
        PolicyRecord(
            tenant_id=tenant,
            subject="*",
            resource="skills",
            action="approve",
            effect="deny",
            condition={"amount": {"gt": 1000}},
            description="大额审批需冻结",
        )
    )
    engine = PolicyEngine(repo=repo)

    denied = await engine.evaluate(
        subject="manager-1",
        resource="skills",
        action="approve",
        context={"role": "manager", "amount": 2000},
        tenant_id=tenant,
    )
    assert denied.allowed is False
    assert denied.hit_policy_id is not None

    allowed = await engine.evaluate(
        subject="manager-1",
        resource="skills",
        action="approve",
        context={"role": "manager", "amount": 100},
        tenant_id=tenant,
    )
    assert allowed.allowed is True


async def test_engine_legacy_condition_still_works():
    tenant = _tenant()
    repo = MemoryPolicyRepository()
    await repo.save_policy(
        PolicyRecord(
            tenant_id=tenant,
            subject="*",
            resource="skills",
            action="approve",
            effect="deny",
            condition={"role": "manager"},
            description="manager 审批冻结",
        )
    )
    engine = PolicyEngine(repo=repo)

    denied = await engine.evaluate(
        subject="manager-1",
        resource="skills",
        action="approve",
        context={"role": "manager"},
        tenant_id=tenant,
    )
    assert denied.allowed is False

    # admin is not "manager", so the ABAC deny does not fire.
    allowed = await engine.evaluate(
        subject="admin-1",
        resource="skills",
        action="approve",
        context={"role": "admin"},
        tenant_id=tenant,
    )
    assert allowed.allowed is True
