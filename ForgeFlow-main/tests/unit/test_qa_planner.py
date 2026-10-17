"""Unit tests for :mod:`forgeflow.qa.planner`."""

from __future__ import annotations

from forgeflow.qa.config import Profile
from forgeflow.qa.discovery import RepositoryIntelligence
from forgeflow.qa.models import TestStatus
from forgeflow.qa.paths import find_project_root
from forgeflow.qa.planner import TestPlanner
from forgeflow.qa.process_runner import ProcessRunner
from forgeflow.qa.task_runner import MakeTaskRunnerAdapter


def _planner() -> TestPlanner:
    root = find_project_root()
    tasks = MakeTaskRunnerAdapter(
        root / "Makefile", "PYX", ProcessRunner(default_timeout_s=5), make_exe="make-x"
    )
    return TestPlanner(root, tasks)


def _inventory():
    return RepositoryIntelligence(find_project_root()).discover(run_id="r")


def test_fast_plan_uses_check_and_targets_exist(monkeypatch):
    monkeypatch.delenv("QA_MAKE_TARGETS", raising=False)
    plan = _planner().plan(_inventory(), profile=Profile.FAST)
    assert [item.task_target for item in plan] == ["check"]
    for item in plan:
        assert item.task_target is not None
        assert item.status is TestStatus.PENDING
        assert item.command  # a real argv was captured
        assert item.layer.value == "BUILD"


def test_full_plan_adds_integration(monkeypatch):
    monkeypatch.delenv("QA_MAKE_TARGETS", raising=False)
    plan = _planner().plan(_inventory(), profile=Profile.FULL)
    assert [item.task_target for item in plan] == ["check", "test-integration"]
    assert {item.test_type.value for item in plan} == {"static", "integration"}


def test_fast_and_full_differ(monkeypatch):
    monkeypatch.delenv("QA_MAKE_TARGETS", raising=False)
    planner = _planner()
    fast = planner.plan(_inventory(), profile=Profile.FAST)
    full = planner.plan(_inventory(), profile=Profile.FULL)
    assert len(full) > len(fast)


def test_override_target_is_honoured(monkeypatch):
    monkeypatch.setenv("QA_MAKE_TARGETS", "lint")
    plan = _planner().plan(_inventory(), profile=Profile.FAST)
    assert [item.task_target for item in plan] == ["lint"]


def test_missing_target_is_inconclusive(monkeypatch):
    monkeypatch.setenv("QA_MAKE_TARGETS", "definitely-not-a-target")
    plan = _planner().plan(_inventory(), profile=Profile.FAST)
    assert plan[0].task_target == "definitely-not-a-target"
    assert plan[0].status is TestStatus.INCONCLUSIVE
    assert plan[0].command == []
