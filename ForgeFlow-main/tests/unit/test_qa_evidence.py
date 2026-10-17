"""Unit tests for :mod:`forgeflow.qa.evidence`."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from forgeflow.qa.config import QaRunConfig
from forgeflow.qa.evidence import EvidenceStore
from forgeflow.qa.models import Evidence, EvidenceStatus, FailureClass


def _config(tmp_path: Path, run_id: str = "run-1") -> QaRunConfig:
    return QaRunConfig(
        project_root=tmp_path,
        artifacts_dir=tmp_path / "qa_tmp" / "qa" / run_id,
        run_id=run_id,
    )


def _evidence(test_id: str = "make:check", run_id: str = "run-1") -> Evidence:
    return Evidence(
        evidence_id=f"{run_id}:{test_id}",
        run_id=run_id,
        capability_id=f"BUILD:{test_id}",
        test_id=test_id,
        command=["make", "CHECK"],
        environment={"cwd": "/x", "env_diff": {}, "profile": "fast"},
        exit_code=0,
        status=EvidenceStatus.PASS,
        stdout_artifact=f"artifacts/{test_id}.stdout.txt",
        stderr_artifact=f"artifacts/{test_id}.stderr.txt",
        attachments=[],
        timestamp=datetime.now(UTC).isoformat(),
        duration_ms=12,
        failure_class=None,
    )


def test_for_run_creates_layout_and_is_immutable(tmp_path):
    cfg = _config(tmp_path)
    store = EvidenceStore.for_run(cfg)
    assert store.run_dir == cfg.artifacts_dir
    assert store.evidence_dir.is_dir()
    assert store.artifacts_dir.is_dir()
    with pytest.raises(RuntimeError):
        EvidenceStore.for_run(cfg)


def test_write_json_refuses_to_overwrite(tmp_path):
    store = EvidenceStore.for_run(_config(tmp_path))
    path = store.write_json("execution_summary.json", {"total": 1})
    assert path.is_file()
    with pytest.raises(FileExistsError):
        store.write_json("execution_summary.json", {"total": 2})


def test_save_and_list_evidence_roundtrip(tmp_path):
    store = EvidenceStore.for_run(_config(tmp_path))
    store.save_evidence(_evidence("make:check"))
    store.save_evidence(_evidence("make:test-unit"))
    records = store.list_evidence()
    assert {r.test_id for r in records} == {"make:check", "make:test-unit"}
    assert all(r.status is EvidenceStatus.PASS for r in records)


def test_write_bytes_and_text(tmp_path):
    store = EvidenceStore.for_run(_config(tmp_path))
    store.write_bytes("artifacts/make_check.stdout.txt", b"raw-bytes")
    store.write_text("final_report.md", "# report\n")
    assert (store.run_dir / "artifacts" / "make_check.stdout.txt").read_bytes() == b"raw-bytes"
    assert (store.run_dir / "final_report.md").read_text(encoding="utf-8").startswith("# report")


def test_write_outside_run_directory_is_rejected(tmp_path):
    store = EvidenceStore.for_run(_config(tmp_path))
    with pytest.raises(ValueError):
        store.write_bytes("../escape.txt", b"nope")


def test_saved_evidence_preserves_all_schema_fields(tmp_path):
    store = EvidenceStore.for_run(_config(tmp_path))
    ev = _evidence("make:check")
    ev.failure_class = FailureClass.UNKNOWN
    store.save_evidence(ev)
    reloaded = store.list_evidence()[0]
    assert reloaded.evidence_id == ev.evidence_id
    assert reloaded.run_id == ev.run_id
    assert reloaded.capability_id == ev.capability_id
    assert reloaded.command == ev.command
    assert reloaded.exit_code == 0
    assert reloaded.duration_ms == 12
    assert reloaded.failure_class is FailureClass.UNKNOWN
    assert reloaded.stdout_artifact == "artifacts/make:check.stdout.txt"


def test_for_run_rejects_pre_existing_directory(tmp_path):
    target = tmp_path / "qa_tmp" / "qa" / "run-1"
    target.mkdir(parents=True)
    with pytest.raises(RuntimeError):
        EvidenceStore.for_run(_config(tmp_path))
