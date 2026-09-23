"""Validation Layer + Replan ceiling (docs §6.6 / P0-07)."""

from __future__ import annotations

from forgeflow.validation.replan import decide_replan, record_replan_event, verdict_from
from forgeflow.validation.validator import validate


def test_validate_success_status():
    verdict = validate({"status": "completed", "steps": [{"tool": "x"}]})
    assert verdict.success is True
    assert verdict.outcome == "success"


def test_validate_failure_with_errors():
    verdict = validate({"status": "completed", "steps": [{"tool": "x"}], "errors": ["boom"]})
    assert verdict.success is False
    assert verdict.outcome == "failure"
    assert any("boom" in r for r in verdict.reasons)


def test_validate_implicit_success_from_steps():
    verdict = validate({"steps": [{"tool": "x"}]})
    assert verdict.success is True


def test_validate_no_steps_no_status_is_failure():
    assert validate({}).success is False


def test_decide_replan_retries_before_ceiling():
    verdict = validate({"status": "failed", "errors": ["x"]})
    decision = decide_replan(verdict, attempt=0, max_attempts=2)
    assert decision.should_replan is True
    assert decision.escalate_hitl is False


def test_max_attempts_escalates_to_hitl():
    verdict = validate({"status": "failed", "errors": ["x"]})
    decision = decide_replan(verdict, attempt=2, max_attempts=2)
    assert decision.should_replan is False
    assert decision.escalate_hitl is True
    assert "ceiling" in decision.reason

    event = record_replan_event(decision)
    assert event["event"] == "replan"
    assert event["escalate_hitl"] is True


def test_decide_replan_noop_on_success():
    decision = decide_replan(validate({"status": "completed", "steps": [{"tool": "x"}]}), attempt=0)
    assert decision.should_replan is False
    assert decision.escalate_hitl is False


def test_verdict_from_accepts_verdict_or_run():
    verdict = validate({"status": "completed", "steps": [{"tool": "x"}]})
    assert verdict_from(verdict) is verdict  # already a Verdict
    assert verdict_from({"steps": [{"tool": "x"}]}).success is True
