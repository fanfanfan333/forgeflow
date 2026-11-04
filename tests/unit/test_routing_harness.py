"""Phase 6: routing eval harness — freeze, scoring, tiers, artifacts.

The SalesOps eval package (lead qualification + LLM judge) is a
different system; these tests pin the routing harness only.
"""

import json
from pathlib import Path

import pytest

from forgeflow.evaluation.routing_harness import (
    DatasetManifest,
    RoutingCase,
    deterministic_predict,
    freeze_manifest,
    load_routing_cases,
    pg_index_row,
    run_tier,
    sample_cases,
    score_routing_cases,
    verify_manifest,
    write_artifacts,
)

DATASET = Path(__file__).resolve().parents[1] / "data" / "supervisor_routing_cases.json"


def _case(case_id, expected, acceptable=None, requires_approval=False):
    return RoutingCase(
        id=case_id,
        task_text="test task",
        stage="qualify",
        score=None,
        approval_status=None,
        expected_route=expected,
        acceptable_routes=tuple(acceptable or [expected]),
        rationale="",
        difficulty="easy",
        requires_approval=requires_approval,
        needs_clarification=False,
        source="synthetic",
    )


# --- dataset + manifest ----------------------------------------------------- #


def test_real_dataset_loads_60_with_unique_ids():
    cases = load_routing_cases(DATASET)
    assert len(cases) == 60
    assert len({case.id for case in cases}) == 60


def test_freeze_then_verify_passes():
    manifest = freeze_manifest(DATASET)
    assert manifest.version == "routing-v1" and manifest.case_count == 60
    assert manifest.route_counts == {
        "researcher": 20,
        "analyzer": 15,
        "executor": 15,
        "human_approval": 5,
        "FINISH": 5,
    }
    verify_manifest(manifest, DATASET, load_routing_cases(DATASET))


def test_verify_accepts_crlf_checkout(tmp_path):
    """A Windows autocrlf checkout must still match the LF manifest."""
    manifest = freeze_manifest(DATASET)
    crlf = tmp_path / "crlf.json"
    normalized = DATASET.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    crlf.write_bytes(normalized.replace(b"\n", b"\r\n"))
    verify_manifest(manifest, crlf, load_routing_cases(crlf))


def test_verify_catches_hash_drift(tmp_path):
    manifest = freeze_manifest(DATASET)
    raw = json.loads(DATASET.read_text(encoding="utf-8"))
    raw[0]["rationale"] = "tampered"
    drifted = tmp_path / "drifted.json"
    drifted.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="sha256 mismatch"):
        verify_manifest(manifest, drifted, load_routing_cases(drifted))


def test_verify_catches_count_drift():
    manifest = freeze_manifest(DATASET)
    cases = load_routing_cases(DATASET)[:59]
    with pytest.raises(ValueError, match="case count mismatch"):
        verify_manifest(manifest, DATASET, cases)


def test_manifest_round_trip():
    manifest = freeze_manifest(DATASET)
    assert DatasetManifest.from_dict(manifest.to_dict()) == manifest


# --- scoring (mirrors the standalone checker) ------------------------------- #


def test_scoring_accuracy_acceptable_and_missed_approval():
    cases = [
        _case("c1", "researcher"),
        _case("c2", "analyzer", acceptable=["analyzer", "researcher"]),
        _case("c3", "human_approval", requires_approval=True),
    ]
    report = score_routing_cases(cases, {"c1": "researcher", "c2": "researcher", "c3": "executor"})
    assert report["accuracy"] == pytest.approx(1 / 3)
    # c2 predicted an acceptable (not expected) route: acceptable, not correct.
    assert report["acceptable_rate"] == pytest.approx(2 / 3)
    # c3 skipped the required human stop.
    assert report["missed_approval_rate"] == 1.0
    assert report["failed_ids"] == ["c2", "c3"]
    assert report["confusion_matrix"]["analyzer"] == {"researcher": 1}


def test_missed_approval_only_counts_expected_human_approval():
    # requires_approval + expected executor + predicted human_approval
    # is over-cautious, not a missed stop.
    cases = [_case("c1", "executor", requires_approval=True)]
    report = score_routing_cases(cases, {"c1": "human_approval"})
    assert report["missed_approval_rate"] == 0.0


def test_timeout_excluded_from_timeout_accuracy_but_not_from_accuracy():
    cases = [_case("c1", "researcher"), _case("c2", "analyzer")]
    report = score_routing_cases(cases, {"c1": "researcher", "c2": "TIMEOUT"})
    assert report["accuracy"] == 0.5
    assert report["accuracy_excluding_timeout"] == 1.0
    assert report["timeout_count"] == 1


# --- tiers ------------------------------------------------------------------- #


def test_tier_a_deterministic_is_60_60_gate_pass():
    cases = load_routing_cases(DATASET)
    rows, report = run_tier(cases, deterministic_predict, tier="a")
    assert len(rows) == 60
    assert report["accuracy"] == 1.0
    assert report["missed_approval_rate"] == 0.0
    assert report["gate"] == "pass" and report["alert"] is False


def test_tier_a_gate_fails_on_any_miss():
    cases = [_case("c1", "researcher"), _case("c2", "analyzer")]
    _, report = run_tier(cases, lambda case: "executor", tier="a")
    assert report["gate"] == "fail"


def test_tier_b_alert_only_never_gates():
    cases = [_case(f"c{i}", "researcher") for i in range(10)]
    # 5/10 correct: below 1.0 - 0.05, so alert — but never a gate fail.
    calls = {"n": 0}

    def flaky(case):
        calls["n"] += 1
        return "researcher" if calls["n"] % 2 == 0 else "executor"

    _, report = run_tier(cases, flaky, tier="b", baseline_accuracy=1.0, tolerance=0.05)
    assert report["accuracy"] == 0.5
    assert report["alert"] is True
    assert report["gate"] == "alert-only"


def test_tier_b_no_alert_within_tolerance():
    cases = [_case(f"c{i}", "researcher") for i in range(20)]
    _, report = run_tier(cases, lambda case: "researcher", tier="b")
    assert report["alert"] is False


def test_sample_is_deterministic_for_a_seed():
    cases = load_routing_cases(DATASET)
    first = [case.id for case in sample_cases(cases, 20, seed=7)]
    second = [case.id for case in sample_cases(cases, 20, seed=7)]
    assert first == second and len(first) == 20
    oversampled = sample_cases(cases, 999, seed=0)
    assert len(oversampled) == 60
    assert {case.id for case in oversampled} == {case.id for case in cases}


# --- artifacts + PG index ---------------------------------------------------- #


def test_artifacts_and_pg_index_row(tmp_path):
    cases = [_case("c1", "researcher"), _case("c2", "analyzer")]
    rows, report = run_tier(cases, lambda case: case.expected_route, tier="a")
    paths = write_artifacts(
        tmp_path,
        run_id="eval-test-1",
        dataset_version="routing-v1",
        tier="a",
        rows=rows,
        report=report,
    )
    lines = Path(paths["cases"]).read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["pass"] is True
    summary = json.loads(Path(paths["summary"]).read_text(encoding="utf-8"))
    assert summary["run_id"] == "eval-test-1" and summary["accuracy"] == 1.0
    index = pg_index_row(summary)
    assert index == {
        "run_id": "eval-test-1",
        "baseline_commit": summary["baseline_commit"],
        "dataset_version": "routing-v1",
        "tier": "a",
        "pass_rate": 1.0,
        "failed_case_ids": [],
    }
