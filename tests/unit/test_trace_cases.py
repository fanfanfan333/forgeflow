"""Phase 9 B5 — failed-trace → eval-case conversion."""

from __future__ import annotations

import json

from forgeflow.evaluation.trace_cases import append_trace_case, trace_to_case


def test_trace_case_keeps_tools_statuses_and_error_signatures_only() -> None:
    case = trace_to_case(
        "run-1",
        [
            {"tool": "code.execute", "status": "error", "payload": {"diff": "SECRET-DIFF"}},
            {"tool": "report.render", "status": "ok", "payload": {"text": "model words"}},
        ],
        ["boom" * 200],
        notes="nightly failure",
    )
    assert case["id"] == "trace-run-1"
    assert case["failed_tools"] == ["code.execute"]
    assert case["steps"] == [
        {"tool": "code.execute", "status": "error"},
        {"tool": "report.render", "status": "ok"},
    ]
    assert len(case["error_signatures"][0]) == 500
    assert "SECRET-DIFF" not in json.dumps(case)
    assert "model words" not in json.dumps(case)
    assert case["notes"] == "nightly failure"


def test_append_trace_case_writes_jsonl(tmp_path) -> None:
    path = append_trace_case(trace_to_case("run-2", [], ["e1"]), tmp_path / "cases.jsonl")
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    assert json.loads(lines[0])["source_run_id"] == "run-2"
