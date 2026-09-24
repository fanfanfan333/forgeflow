"""INC9 B1 — skill canary pure layer (state machine, A/B decision, exposure).

Covers docs/sop/12-INC9-DESIGN.md §10 (unit, pure functions): every
``decide_ab`` branch (insufficient / no_baseline / regression / warning / ok),
legality of ``next_state``, determinism of ``should_serve``, and the default
(zero-change) edge of the exposure primitive.
"""

from __future__ import annotations

import pytest

from forgeflow.skills.canary import (
    ABDecision,
    CanaryError,
    CanaryState,
    decide_ab,
    next_state,
    should_serve,
)

# --------------------------------------------------------------------------- #
# state machine                                                                #
# --------------------------------------------------------------------------- #

def test_candidate_promotes_directly_when_canary_is_off():
    assert next_state(CanaryState.CANDIDATE, "promote") is CanaryState.PROMOTED


def test_candidate_can_start_canary():
    assert next_state(CanaryState.CANDIDATE, "start_canary") is CanaryState.CANARY


def test_canary_promotes_on_win():
    assert next_state(CanaryState.CANARY, "promote") is CanaryState.PROMOTED


def test_canary_rolls_back_on_regression():
    assert next_state(CanaryState.CANARY, "rollback") is CanaryState.ROLLED_BACK


def test_canary_holds_keeps_state():
    assert next_state(CanaryState.CANARY, "hold") is CanaryState.CANARY


def test_promoted_can_be_rolled_back():
    assert next_state(CanaryState.PROMOTED, "rollback") is CanaryState.ROLLED_BACK


def test_rolled_back_can_be_re_promoted():
    assert next_state(CanaryState.ROLLED_BACK, "promote") is CanaryState.PROMOTED


@pytest.mark.parametrize(
    "state,event",
    [
        (CanaryState.CANDIDATE, "rollback"),
        (CanaryState.CANDIDATE, "hold"),
        (CanaryState.PROMOTED, "promote"),
        (CanaryState.ROLLED_BACK, "rollback"),
        (CanaryState.CANARY, "bogus"),
    ],
)
def test_illegal_transitions_fail_closed(state, event):
    with pytest.raises(CanaryError):
        next_state(state, event)


# --------------------------------------------------------------------------- #
# controlled exposure                                                          #
# --------------------------------------------------------------------------- #

def test_should_serve_is_false_at_zero_pct():
    # Every seed is refused at the default ⇒ selection layer unchanged.
    assert should_serve("anything", 0) is False
    assert should_serve("another", 0) is False


def test_should_serve_is_true_at_full_pct():
    assert should_serve("anything", 100) is True


def test_should_serve_is_deterministic():
    a = should_serve("tenant:skill:run-1", 40)
    b = should_serve("tenant:skill:run-1", 40)
    assert a == b


def test_should_serve_roughly_tracks_pct():
    seeds = [f"seed-{i}" for i in range(2000)]
    served = sum(1 for s in seeds if should_serve(s, 30))
    # sha1 is uniform enough that a 30% bucket lands well inside ±5pp.
    assert 500 <= served <= 700


def test_should_serve_negative_pct_is_off():
    assert should_serve("x", -5) is False


# --------------------------------------------------------------------------- #
# decide_ab                                                                    #
# --------------------------------------------------------------------------- #

def _ok_pair():
    return {"score": 0.90}, {"score": 0.89}


def test_hold_when_samples_below_min():
    decision = decide_ab({"score": 0.99}, {"score": 0.50}, sample_n=3, min_samples=10)
    assert isinstance(decision, ABDecision)
    assert decision.action == "hold"
    assert decision.severity == "insufficient"
    assert decision.allowed is False


def test_hold_when_no_baseline():
    decision = decide_ab({"score": 0.99}, None, sample_n=50, min_samples=10)
    assert decision.action == "hold"
    assert decision.severity == "no_baseline"
    assert decision.baseline_present is False


def test_hold_when_baseline_present_but_no_overlap():
    decision = decide_ab(
        {"unrelated": 1.0}, {"score": 0.9}, sample_n=50, min_samples=10
    )
    assert decision.action == "hold"
    assert decision.severity == "no_baseline"
    assert decision.baseline_present is True


def test_rollback_on_regression():
    decision = decide_ab({"score": 0.60}, {"score": 0.90}, sample_n=50, min_samples=10)
    assert decision.action == "rollback"
    assert decision.severity == "regression"
    assert decision.allowed is False
    assert decision.findings  # the regression is explained


def test_promote_on_ok():
    canary, incumbent = _ok_pair()
    decision = decide_ab(canary, incumbent, sample_n=50, min_samples=10)
    assert decision.action == "promote"
    assert decision.severity == "ok"
    assert decision.allowed is True


def test_promote_on_warning():
    # Small slip: past the warn tolerance (0.02) but inside the fail one (0.05).
    decision = decide_ab({"score": 0.86}, {"score": 0.90}, sample_n=50, min_samples=10)
    assert decision.action == "promote"
    assert decision.severity == "warning"
    assert decision.allowed is True


def test_zero_min_samples_disables_the_floor():
    decision = decide_ab({"score": 0.90}, {"score": 0.89}, sample_n=0, min_samples=0)
    assert decision.action == "promote"


def test_decide_ab_uses_the_same_tolerance_table_as_the_release_gate():
    from forgeflow.skills.release_gate import RELEASE_TOLERANCES

    # score fail-tolerance is 0.05 ⇒ a 0.06 drop is a regression, a 0.04 drop is not.
    assert RELEASE_TOLERANCES["score"] == ("higher_is_better", 0.02, 0.05)
    regressed = decide_ab({"score": 0.84}, {"score": 0.90}, sample_n=50, min_samples=10)
    slipped = decide_ab({"score": 0.86}, {"score": 0.90}, sample_n=50, min_samples=10)
    assert regressed.action == "rollback"
    assert slipped.action == "promote"


def test_decision_is_serialisable():
    decision = decide_ab({"score": 0.95}, {"score": 0.90}, sample_n=20, min_samples=10)
    payload = decision.to_dict()
    assert payload["action"] == "promote"
    assert payload["severity"] == "ok"
    assert payload["sample_n"] == 20
