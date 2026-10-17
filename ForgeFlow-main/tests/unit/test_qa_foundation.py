"""Unit tests for the QA foundation layer (models / paths / config)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from forgeflow.qa.config import (
    FAST_PROFILE_TARGETS,
    FULL_PROFILE_TARGETS,
    Profile,
    QaRunConfig,
    profile_targets,
)
from forgeflow.qa.models import (
    ALL_LAYERS,
    Capability,
    CapabilityInventory,
    EvidenceStatus,
    FailureClass,
    Layer,
    ProcessStatus,
    TestPlanItem,
    TestType,
    Verdict,
)
from forgeflow.qa.paths import QaPaths, find_project_root

_RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")


def test_failure_class_has_14_members():
    assert len(list(FailureClass)) == 14
    assert FailureClass.ENVIRONMENT_FAILURE == "ENVIRONMENT_FAILURE"
    assert FailureClass.UNKNOWN.value == "UNKNOWN"


def test_layer_has_19_members_and_all_layers_matches():
    assert len(list(Layer)) == 19
    assert tuple(Layer) == ALL_LAYERS
    assert Layer.CORE in ALL_LAYERS and Layer.DEPLOYMENT in ALL_LAYERS


def test_evidence_status_and_verdict_vocabularies():
    assert {s.value for s in EvidenceStatus} == {
        "PASS",
        "FAIL",
        "BLOCKED",
        "INCONCLUSIVE",
        "SKIP",
    }
    assert Verdict.PASS_WITH_WARNINGS == "PASS_WITH_WARNINGS"


def test_process_status_vocabulary():
    assert {s.value for s in ProcessStatus} == {"ok", "failed", "timeout", "spawn_error"}


def test_capability_defaults_and_confidence_bounds():
    cap = Capability(
        capability_id="CORE:forgeflow/config.py",
        layer=Layer.CORE,
        name="config",
        path="forgeflow/config.py",
    )
    assert cap.confidence == 1.0
    assert cap.verified is False
    assert cap.entrypoints == [] and cap.detected_by == []
    with pytest.raises(ValueError):
        Capability(
            capability_id="x",
            layer=Layer.CORE,
            name="n",
            path="p",
            confidence=2.0,
        )


def test_test_plan_item_defaults_are_frozen_per_instance():
    first = TestPlanItem(
        test_id="t1",
        capability_id="c1",
        layer=Layer.BUILD,
        test_type=TestType.UNIT,
        description="d",
    )
    second = TestPlanItem(
        test_id="t2",
        capability_id="c2",
        layer=Layer.BUILD,
        test_type=TestType.UNIT,
        description="d",
    )
    first.requires.append("pg")
    assert second.requires == []  # no shared mutable default


def test_capability_inventory_roundtrips_through_json():
    inv = CapabilityInventory(
        run_id="r",
        generated_at=datetime.now(UTC).isoformat(),
        root="/tmp/x",
        layers={Layer.CORE: [], Layer.BUILD: []},
        totals={"CORE": 0, "BUILD": 0, "_all": 0},
    )
    restored = CapabilityInventory.model_validate_json(inv.model_dump_json())
    assert set(restored.layers) == {Layer.CORE, Layer.BUILD}


def test_new_run_id_shape_is_sortable_and_unique():
    moment = datetime(2026, 10, 8, 3, 10, 14, tzinfo=UTC)
    run_id = QaPaths.new_run_id(moment)
    assert _RUN_ID_RE.match(run_id)
    assert run_id.startswith("20261008T031014Z-")
    assert QaPaths.new_run_id(moment) != QaPaths.new_run_id(moment)


def test_find_project_root_locates_forgeflow_main():
    root = find_project_root()
    assert (root / "pyproject.toml").is_file()
    assert (root / "forgeflow" / "__init__.py").is_file()
    assert root.name == "ForgeFlow-main"


def test_find_project_root_ignores_cwd(tmp_path, monkeypatch):
    # Anchoring on the package file must be independent of the working directory.
    monkeypatch.chdir(tmp_path)
    assert find_project_root() == find_project_root(Path(__file__))


def test_find_project_root_env_override_and_invalid(monkeypatch, tmp_path):
    real = find_project_root()
    monkeypatch.setenv("QA_PROJECT_ROOT", str(real))
    assert find_project_root() == real
    monkeypatch.setenv("QA_PROJECT_ROOT", str(tmp_path))
    with pytest.raises(RuntimeError):
        find_project_root()


def test_qapaths_resolve_from_repo_root_and_tmp_override(tmp_path):
    repo_root = find_project_root().parent
    paths = QaPaths.resolve(repo_root)
    assert paths.project_root == find_project_root()
    assert paths.tmp_root == find_project_root() / "qa_tmp"
    assert paths.run_dir("run-1") == find_project_root() / "qa_tmp" / "qa" / "run-1"

    override = QaPaths(find_project_root(), tmp_env=str(tmp_path / "elsewhere"))
    assert override.tmp_root == tmp_path / "elsewhere"
    assert override.run_dir("run-2") == tmp_path / "elsewhere" / "qa" / "run-2"


def test_qapaths_resolve_rejects_non_project(tmp_path):
    with pytest.raises(RuntimeError):
        QaPaths.resolve(tmp_path)


def test_profile_targets_default_and_override(monkeypatch):
    monkeypatch.delenv("QA_MAKE_TARGETS", raising=False)
    assert profile_targets(Profile.FAST) == FAST_PROFILE_TARGETS == ("check",)
    assert profile_targets(Profile.FULL) == FULL_PROFILE_TARGETS
    monkeypatch.setenv("QA_MAKE_TARGETS", "lint, test-unit")
    assert profile_targets(Profile.FAST) == ("lint", "test-unit")


def test_qa_run_config_env_overrides(tmp_path):
    cfg = QaRunConfig(
        project_root=find_project_root(),
        artifacts_dir=tmp_path / "run",
        run_id="r",
        extra_env={"FOO": "bar"},
    )
    overrides = cfg.env_overrides()
    assert overrides["STORAGE_BACKEND"] == "memory"
    assert overrides["LLM_PROVIDER"] == "mock"
    assert overrides["FOO"] == "bar"
    full_env = cfg.subprocess_env()
    assert full_env["STORAGE_BACKEND"] == "memory" and full_env["FOO"] == "bar"
