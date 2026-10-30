"""Supervisor route normalization (2026-10-30 routing run b81d9c4).

The model returned "research" at sup-r-02 and the whole evaluation
aborted; in production a Literal rejection would crash the graph.
A stage name is a predictable slip — normalize it, never crash on it.
"approve" stays rejected: it is ambiguous between executor (approve
stage) and human_approval (a different worker).
"""

import pytest
from pydantic import ValidationError

from forgeflow.agents.supervisor import RoutingDecision, normalize_route


def test_stage_names_normalize():
    assert normalize_route("research") == "researcher"
    assert normalize_route("analyze") == "analyzer"
    assert normalize_route("execute") == "executor"
    assert normalize_route("FINISH") == "FINISH"
    assert normalize_route("researcher") == "researcher"


def test_routing_decision_accepts_stage_name():
    decision = RoutingDecision(next="research", reasoning="gather")
    assert decision.next == "researcher"


def test_routing_decision_rejects_ambiguous_approve():
    with pytest.raises(ValidationError):
        RoutingDecision(next="approve", reasoning="x")


def test_evaluator_does_not_abort_on_invalid_route():
    from scripts.evaluate_supervisor_routing import score_cases

    cases = [
        {
            "id": "c1",
            "expected_route": "researcher",
            "acceptable_routes": ["researcher"],
            "task_text": "t",
            "stage": "qualify",
        }
    ]
    report = score_cases(cases, {"c1": "INVALID:weird"})
    assert report["total"] == 1
    assert report["correct"] == 0
    assert report["errors"][0]["predicted"] == "INVALID:weird"
