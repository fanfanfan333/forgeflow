"""Supervisor routing dataset: schema, balance, and deterministic baseline.

The dataset is a labelled evaluation asset, not a model claim.
The deterministic evaluator mirrors the stage mapping documented in
``SupervisorAgent``'s system prompt, so it is expected to score 60/60:
any disagreement is a dataset authoring error. The LLM mode (local
Ollama qwen3:8b) is measured separately, on the user's machine, and
its figure is never mixed into this baseline.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

DATASET = Path(__file__).resolve().parents[1] / "data" / "supervisor_routing_cases.json"
EVALUATOR = Path(__file__).resolve().parents[2] / "scripts" / "evaluate_supervisor_routing.py"


def _load_evaluator():
    spec = importlib.util.spec_from_file_location("evaluate_supervisor_routing", EVALUATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dataset_has_60_cases_with_unique_ids_and_balanced_classes():
    cases = json.loads(DATASET.read_text(encoding="utf-8"))
    assert len(cases) == 60
    assert len({case["id"] for case in cases}) == 60
    expected_counts = {
        "researcher": 20,
        "analyzer": 15,
        "executor": 15,
        "human_approval": 5,
        "FINISH": 5,
    }
    counts: dict[str, int] = {}
    for case in cases:
        counts[case["expected_route"]] = counts.get(case["expected_route"], 0) + 1
    assert counts == expected_counts


def test_dataset_fields_are_complete_and_internally_consistent():
    evaluator = _load_evaluator()
    cases = evaluator.load_cases(DATASET)
    assert len(cases) == 60
    for case in cases:
        assert case["expected_route"] in case["acceptable_routes"]
        assert case["difficulty"] in {"easy", "ambiguous", "multi-step"}
        assert isinstance(case["requires_approval"], bool)
        assert isinstance(case["needs_clarification"], bool)
        assert case["source"] == "synthetic"
        if case["expected_route"] == "human_approval":
            assert case["requires_approval"] is True
        if case["stage"] == "analyze" and case["expected_route"] == "executor":
            assert case["score"] is not None and case["score"] >= 4.0
        if case["stage"] == "analyze" and case["expected_route"] == "FINISH":
            assert case["score"] is not None and case["score"] < 4.0


def test_deterministic_baseline_is_consistent_with_the_documented_mapping():
    evaluator = _load_evaluator()
    cases = evaluator.load_cases(DATASET)
    predictions = {case["id"]: evaluator.deterministic_route(case) for case in cases}
    report = evaluator.score_cases(cases, predictions)
    assert report["accuracy"] == 1.0
    assert report["acceptable_rate"] == 1.0
    assert report["missed_approval_rate"] == 0.0
    assert report["errors"] == []


def test_score_cases_reports_missed_approval_separately_from_accuracy():
    evaluator = _load_evaluator()
    cases = [
        {
            "id": "x-1",
            "task_text": "提交提案等待审批",
            "stage": "propose",
            "score": None,
            "approval_status": None,
            "expected_route": "human_approval",
            "acceptable_routes": ["human_approval"],
            "rationale": "审批",
            "difficulty": "ambiguous",
            "requires_approval": True,
            "needs_clarification": False,
            "source": "synthetic",
        },
        {
            "id": "x-2",
            "task_text": "收集公司信息",
            "stage": "qualify",
            "score": None,
            "approval_status": None,
            "expected_route": "researcher",
            "acceptable_routes": ["researcher"],
            "rationale": "调研",
            "difficulty": "easy",
            "requires_approval": False,
            "needs_clarification": False,
            "source": "synthetic",
        },
    ]
    # Skipping the required human stop: one miss out of one approval case,
    # even though the other case is correct (accuracy 0.5, missed 1.0).
    report = evaluator.score_cases(cases, {"x-1": "executor", "x-2": "researcher"})
    assert report["accuracy"] == 0.5
    assert report["missed_approval_rate"] == 1.0
    assert report["confusion_matrix"]["human_approval"] == {"executor": 1}
