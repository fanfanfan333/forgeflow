"""End-to-end: ``python -m forgeflow.qa full --profile fast`` (§8.3 / T13).

This drives the whole Phase A–C loop for real and asserts the nine artifact
kinds are produced. It runs the pipeline against the ``lint`` Make target rather
than the production ``check`` target on purpose: ``check`` runs the entire test
suite, which would re-enter this very test (recursion). ``lint`` is a real,
cheap target that exercises the same discover → plan → execute → analyze →
report path end to end, and honours the exact same evidence contract.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from forgeflow.qa.cli import main

_TOP_LEVEL = (
    "capability_inventory.json",
    "discovery_report.json",
    "test_plan.json",
    "execution_summary.json",
    "failure_analysis.json",
    "final_report.md",
)


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
def test_full_fast_produces_all_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("QA_MAKE_TARGETS", "lint")
    monkeypatch.setenv("QA_MAKE", _MAKE)
    run_dir = tmp_path / "run"

    rc = main(["full", "--profile", "fast", "--artifacts-dir", str(run_dir)])
    assert rc == 0

    for name in _TOP_LEVEL:
        assert (run_dir / name).is_file(), name
    evidence_files = list((run_dir / "evidence").glob("*.json"))
    assert evidence_files
    assert list((run_dir / "artifacts").glob("*.stdout.txt"))
    assert list((run_dir / "artifacts").glob("*.stderr.txt"))

    summary = json.loads((run_dir / "execution_summary.json").read_text(encoding="utf-8"))
    assert summary["total"] == 1
    assert summary["results"][0]["exit_code"] == 0

    evidence = json.loads(evidence_files[0].read_text(encoding="utf-8"))
    assert evidence["status"] == "PASS"
    assert evidence["exit_code"] == 0
    assert evidence["command"][-1] == "lint"
    assert evidence["stdout_artifact"].startswith("artifacts/")

    inventory = json.loads((run_dir / "capability_inventory.json").read_text(encoding="utf-8"))
    assert len(inventory["layers"]) == 19

    report = (run_dir / "final_report.md").read_text(encoding="utf-8")
    assert "verdict: PASS" in report
