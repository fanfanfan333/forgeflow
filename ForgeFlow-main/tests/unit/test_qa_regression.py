"""Unit tests for :mod:`forgeflow.qa.regression` (Phase D §20/§69).

Covers the affected-surface evaluation (explicit changes, global-surface fallback,
failing-evidence fallback, the honest "no signal" case), the metric computation,
the reused baseline comparison (with and without a baseline) and the Wilson lower
bound. The "first run is not a regression" rule is pinned directly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from forgeflow.qa.models import Evidence, EvidenceStatus, FailureClass
from forgeflow.qa.paths import find_project_root
from forgeflow.qa.regression import (
    QA_METRIC_TOLERANCES,
    RegressionAnalyzer,
    changed_files_from_env,
)

_ROOT = find_project_root()


def _ev(
    test_id: str, status: EvidenceStatus, failure_class: FailureClass | None = None
) -> Evidence:
    return Evidence(
        evidence_id=f"r:{test_id}",
        run_id="r",
        capability_id="BUILD:check",
        test_id=test_id,
        command=["make", "PY=x", "check"],
        exit_code=0 if status is EvidenceStatus.PASS else 1,
        status=status,
        timestamp=datetime.now(UTC).isoformat(),
        failure_class=failure_class,
    )


def _analyzer(monkeypatch: pytest.MonkeyPatch, *, baseline=None) -> RegressionAnalyzer:
    monkeypatch.delenv("QA_CHANGED_FILES", raising=False)
    if baseline is None:
        monkeypatch.delenv("QA_REGRESSION_BASELINE", raising=False)
    else:
        monkeypatch.setenv("QA_REGRESSION_BASELINE", str(baseline))
    return RegressionAnalyzer(_ROOT)


# ── changed_files_from_env ───────────────────────────────────────────────────


def test_changed_files_from_env_parsing(monkeypatch):
    monkeypatch.setenv("QA_CHANGED_FILES", "forgeflow/qa/a.py,\n ./forgeflow/qa/b.py ,, ")
    assert changed_files_from_env() == ["forgeflow/qa/a.py", "forgeflow/qa/b.py"]


def test_changed_files_from_env_normalises_backslashes(monkeypatch):
    monkeypatch.setenv("QA_CHANGED_FILES", "forgeflow\\qa\\a.py")
    assert changed_files_from_env() == ["forgeflow/qa/a.py"]


def test_changed_files_from_env_unset(monkeypatch):
    monkeypatch.delenv("QA_CHANGED_FILES", raising=False)
    assert changed_files_from_env() == []


# ── scope (§20) ──────────────────────────────────────────────────────────────


def test_scope_from_explicit_non_global_changes(monkeypatch):
    analyzer = _analyzer(monkeypatch)
    scope = analyzer.scope(changed_files=["forgeflow/api/routers/runs.py", "tests/unit/test_x.py"])
    assert set(scope.affected_routes) == {"runs"}
    assert "forgeflow" in scope.affected_packages
    assert scope.full_repo_fallback is False
    assert scope.recommended_targets
    assert all(analyzer.has_target(target) for target in scope.recommended_targets)
    assert scope.confidence > 0.0


def test_scope_global_surface_forces_full_repo(monkeypatch):
    analyzer = _analyzer(monkeypatch)
    scope = analyzer.scope(changed_files=["pyproject.toml", "forgeflow/qa/models.py"])
    assert scope.full_repo_fallback is True
    assert scope.recommended_targets == ["check"]


def test_scope_without_any_signal_is_empty_and_honest(monkeypatch):
    analyzer = _analyzer(monkeypatch)
    scope = analyzer.scope(changed_files=[])
    assert scope.changed_files == []
    assert scope.recommended_targets == []
    assert scope.full_repo_fallback is False
    assert scope.confidence == 0.0
    assert scope.coverage_note == "no change signal available"


def test_scope_falls_back_to_failing_evidence(monkeypatch):
    analyzer = _analyzer(monkeypatch)
    failing = [_ev("make:check", EvidenceStatus.FAIL, FailureClass.UNKNOWN)]
    scope = analyzer.scope(changed_files=[], failing_evidence=failing)
    assert scope.changed_files == []
    assert scope.affected_tests == ["make:check"]
    assert scope.full_repo_fallback is False
    assert "inferred from failing evidence" in scope.coverage_note


def test_scope_reverse_imports_widen_affected_tests(monkeypatch):
    analyzer = _analyzer(monkeypatch)
    scope = analyzer.scope(changed_files=["forgeflow/api/routers/runs.py"])
    # A real test file that imports the changed module is discovered by the
    # bounded reverse-dependency scan (path ends with the test file name).
    assert any(path.startswith("tests/") for path in scope.affected_tests)


# ── metrics + comparison (§69) ───────────────────────────────────────────────


def test_metrics_pass_rate_none_when_unmeasured(monkeypatch):
    analyzer = _analyzer(monkeypatch)
    metrics = analyzer.metrics([])
    assert metrics["total"] == 0
    assert metrics["pass_rate"] is None


def test_metrics_counts_status_and_environment(monkeypatch):
    analyzer = _analyzer(monkeypatch)
    evidence = [
        _ev("a", EvidenceStatus.PASS),
        _ev("b", EvidenceStatus.FAIL, FailureClass.ENVIRONMENT_FAILURE),
    ]
    metrics = analyzer.metrics(evidence)
    assert metrics["total"] == 2
    assert metrics["pass_rate"] == 0.5
    assert metrics["failure_count"] == 1
    assert metrics["environment_failure_count"] == 1


def test_compare_without_baseline_is_not_a_regression(monkeypatch):
    analyzer = _analyzer(monkeypatch)
    metrics = analyzer.metrics([_ev("a", EvidenceStatus.PASS)])
    assert analyzer.compare(metrics, None) == {"passed": True, "findings": []}


def test_compare_detects_regression_against_baseline(monkeypatch):
    analyzer = _analyzer(monkeypatch)
    metrics = analyzer.metrics([_ev("a", EvidenceStatus.FAIL, FailureClass.SOURCE_BUG)])
    comparison = analyzer.compare(metrics, {"pass_rate": 1.0, "failure_count": 0})
    assert comparison["passed"] is False
    assert any(finding["severity"] == "regression" for finding in comparison["findings"])


def test_pass_rate_lower_bound_none_when_empty(monkeypatch):
    analyzer = _analyzer(monkeypatch)
    assert analyzer.pass_rate_lower_bound([]) is None


def test_pass_rate_lower_bound_is_below_point_estimate(monkeypatch):
    analyzer = _analyzer(monkeypatch)
    evidence = [_ev(f"t{i}", EvidenceStatus.PASS) for i in range(9)]
    evidence.append(_ev("f", EvidenceStatus.FAIL, FailureClass.UNKNOWN))
    bound = analyzer.pass_rate_lower_bound(evidence)
    assert bound is not None
    assert 0.0 <= bound < 0.9


# ── report (§42) ─────────────────────────────────────────────────────────────


def test_report_without_baseline_is_honest(monkeypatch):
    analyzer = _analyzer(monkeypatch)
    report = analyzer.report(
        run_id="r1",
        changed_files=["forgeflow/api/routers/runs.py"],
        evidence=[_ev("a", EvidenceStatus.PASS)],
    )
    assert report.baseline_available is False
    assert report.baseline_path is None
    assert report.findings == []
    assert report.pass_rate_lower_bound is not None
    assert report.coverage_note


def test_report_with_baseline_reads_file(monkeypatch, tmp_path):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"pass_rate": 1.0, "failure_count": 0}), encoding="utf-8")
    analyzer = _analyzer(monkeypatch, baseline=baseline)
    report = analyzer.report(
        run_id="r2",
        changed_files=["forgeflow/qa/models.py"],
        evidence=[_ev("a", EvidenceStatus.FAIL, FailureClass.SOURCE_BUG)],
    )
    assert report.baseline_available is True
    assert report.baseline_path == str(baseline)
    assert any(finding["severity"] == "regression" for finding in report.findings)


def test_report_missing_baseline_file_is_not_a_regression(monkeypatch, tmp_path):
    analyzer = _analyzer(monkeypatch, baseline=tmp_path / "does-not-exist.json")
    report = analyzer.report(run_id="r3", evidence=[_ev("a", EvidenceStatus.PASS)])
    assert report.baseline_available is False
    assert all(finding["severity"] != "regression" for finding in report.findings)


def test_tolerance_keys_match_metrics_output(monkeypatch):
    analyzer = _analyzer(monkeypatch)
    metrics = analyzer.metrics([_ev("a", EvidenceStatus.PASS)])
    assert set(QA_METRIC_TOLERANCES) <= set(metrics)
