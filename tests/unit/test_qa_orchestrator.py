"""Unit tests for :mod:`forgeflow.qa.orchestrator` (the 7-step loop)."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from forgeflow.qa.config import Profile, QaRunConfig
from forgeflow.qa.models import Verdict
from forgeflow.qa.orchestrator import QAOrchestrator
from forgeflow.qa.paths import find_project_root

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


def _use_cheap_target(monkeypatch) -> None:
    """Drive the pipeline with ``lint`` (a real, cheap target) — no recursion."""
    monkeypatch.setenv("QA_MAKE_TARGETS", "lint")
    make = _resolve_make()
    if make is not None:
        monkeypatch.setenv("QA_MAKE", make)


def _config(tmp_path: Path, run_id: str) -> QaRunConfig:
    return QaRunConfig(
        project_root=find_project_root(),
        artifacts_dir=tmp_path / "qa_tmp" / "qa" / run_id,
        run_id=run_id,
        profile=Profile.FAST,
    )


def test_full_run_writes_expected_artifacts(tmp_path, monkeypatch):
    _use_cheap_target(monkeypatch)
    config = _config(tmp_path, "r1")
    orchestrator = QAOrchestrator(config)
    report = orchestrator.run_stage("full")

    run_dir = config.artifacts_dir
    for name in _TOP_LEVEL:
        assert (run_dir / name).is_file(), name
    assert list((run_dir / "evidence").glob("*.json"))
    assert list((run_dir / "artifacts").glob("*.stdout.txt"))
    assert list((run_dir / "artifacts").glob("*.stderr.txt"))
    assert report == run_dir / "final_report.md"
    assert orchestrator.state["final_summary"]["verdict"] in {v.value for v in Verdict}


def test_inventory_has_19_layers(tmp_path, monkeypatch):
    _use_cheap_target(monkeypatch)
    config = _config(tmp_path, "r2")
    QAOrchestrator(config).run_stage("discover")
    inventory = json.loads(
        (config.artifacts_dir / "capability_inventory.json").read_text(encoding="utf-8")
    )
    assert len(inventory["layers"]) == 19
    assert inventory["totals"]["_all"] > 0


def test_evidence_is_immutable_per_run_id(tmp_path, monkeypatch):
    _use_cheap_target(monkeypatch)
    config = _config(tmp_path, "r3")
    QAOrchestrator(config)
    with pytest.raises(RuntimeError):
        QAOrchestrator(config)


def test_state_tracks_evidence_and_plan(tmp_path, monkeypatch):
    _use_cheap_target(monkeypatch)
    orchestrator = QAOrchestrator(_config(tmp_path, "r4"))
    orchestrator.run_stage("full")
    state = orchestrator.state
    assert state["test_plan"]
    assert state["evidence"]
    assert state["execution_queue"] == [item.test_id for item in state["test_plan"]]
    # D–K fields stay empty in A–C.
    assert state["root_causes"] == [] and state["change_sets"] == []
