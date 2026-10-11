"""End-to-end: ``python -m forgeflow.qa full --profile fast`` (Phase D, §4/§42).

Drives the whole loop against the cheap-but-real ``lint`` target (so the run does
not re-enter this very suite via ``make check``) and asserts the Phase D
artifacts: a new ``regression_report.json``, the deepened ``failure_analysis.json``
and the appended ``final_report.md`` section. The "first run is not a regression"
rule is checked directly: with no baseline configured, no finding may be a
regression.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from forgeflow.qa.cli import main


def _resolve_make() -> str | None:
    for candidate in (os.environ.get("QA_MAKE"), shutil.which("make")):
        if candidate and Path(candidate).exists():
            return candidate
    fallback = Path.home() / ".workbuddy" / "binaries" / "make" / "bin"
    for name in ("make.exe", "make"):
        candidate = fallback / name
        if candidate.exists():
            return str(candidate)
    return None


_MAKE = _resolve_make()


@pytest.mark.skipif(_MAKE is None, reason="make is not available")
def test_full_fast_writes_regression_report_and_deep_analysis(tmp_path, monkeypatch):
    monkeypatch.setenv("QA_MAKE_TARGETS", "lint")
    monkeypatch.setenv("QA_MAKE", _MAKE)
    monkeypatch.delenv("QA_REGRESSION_BASELINE", raising=False)
    monkeypatch.delenv("QA_CHANGED_FILES", raising=False)
    run_dir = tmp_path / "run"

    rc = main(["full", "--profile", "fast", "--artifacts-dir", str(run_dir)])
    assert rc == 0

    # ── the new regression artifact (§42) ────────────────────────────────
    report_path = run_dir / "regression_report.json"
    assert report_path.is_file()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["baseline_available"] is False
    assert report["baseline_path"] is None
    assert isinstance(report["scope"], dict)
    # a first run is never a regression
    assert all(finding.get("severity") != "regression" for finding in report["findings"])

    # ── the deepened failure analysis (§68/§90) ──────────────────────────
    analysis = json.loads((run_dir / "failure_analysis.json").read_text(encoding="utf-8"))
    assert analysis.get("phase") == "D"
    for key in ("fingerprints", "environment_attribution", "root_cause_candidates"):
        assert key in analysis

    # ── the appended report section (§42/§72) ────────────────────────────
    text = (run_dir / "final_report.md").read_text(encoding="utf-8")
    assert "Regression scope" in text
    assert "verdict: PASS" in text
