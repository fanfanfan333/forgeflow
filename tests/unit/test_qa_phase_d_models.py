"""Schema / boundary / serialization tests for the Phase D data models.

Phase D adds four pydantic models to :mod:`forgeflow.qa.models` (the single
authoritative source): ``FailureFingerprint`` / ``RootCauseCandidate`` (§68/§90)
and ``RegressionScope`` / ``RegressionReport`` (§20/§42/§69). These tests pin
their defaults, bounds and JSON round-trip so the on-disk schema cannot drift.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from forgeflow.qa.models import (
    FailureClass,
    FailureFingerprint,
    RegressionReport,
    RegressionScope,
    RootCauseCandidate,
)


def test_failure_fingerprint_defaults_and_serialization():
    fp = FailureFingerprint(fingerprint="abc123", failure_class=FailureClass.SOURCE_BUG)
    assert fp.test_ids == []
    assert fp.count == 0
    assert fp.signature == []
    assert fp.model_dump(mode="json") == {
        "fingerprint": "abc123",
        "failure_class": "SOURCE_BUG",
        "test_ids": [],
        "count": 0,
        "signature": [],
    }


def test_root_cause_candidate_confidence_and_attribution_bounds():
    ok = RootCauseCandidate(
        test_id="t", failure_class=FailureClass.UNKNOWN, fingerprint="f", confidence=1.0
    )
    assert ok.attribution == "unknown"
    assert ok.evidence_excerpt == ""
    with pytest.raises(ValidationError):
        RootCauseCandidate(
            test_id="t", failure_class=FailureClass.UNKNOWN, fingerprint="f", confidence=1.5
        )
    with pytest.raises(ValidationError):
        RootCauseCandidate(
            test_id="t", failure_class=FailureClass.UNKNOWN, fingerprint="f", attribution="nonsense"
        )


def test_regression_scope_defaults():
    scope = RegressionScope()
    assert scope.changed_files == []
    assert scope.affected_modules == []
    assert scope.recommended_targets == []
    assert scope.full_repo_fallback is False
    assert scope.confidence == 0.0
    assert scope.coverage_note == ""


def test_regression_report_roundtrip():
    report = RegressionReport(
        run_id="r1",
        generated_at="2026-10-08T00:00:00+00:00",
        scope=RegressionScope(changed_files=["a.py"]),
        baseline_available=False,
        pass_rate_lower_bound=None,
    )
    payload = report.model_dump(mode="json")
    assert payload["scope"]["changed_files"] == ["a.py"]
    assert payload["baseline_available"] is False
    again = RegressionReport.model_validate(payload)
    assert again.scope.changed_files == ["a.py"]
    assert again.pass_rate_lower_bound is None


def test_regression_report_default_lists_are_independent():
    first = RegressionReport(run_id="a", generated_at="t", scope=RegressionScope())
    second = RegressionReport(run_id="b", generated_at="t", scope=RegressionScope())
    first.findings.append({"severity": "ok"})
    assert second.findings == []
