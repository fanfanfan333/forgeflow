"""Edge tests for :mod:`forgeflow.qa.planner` and :mod:`forgeflow.qa.executor`.

The two invariants that make the plan honest:

* a target that is **not** in the Makefile is planned ``INCONCLUSIVE`` and is
  **never executed** (§15);
* evidence ``status`` is derived from the *real* process outcome — an exit-0 run
  that collected nothing is ``INCONCLUSIVE``, not ``PASS`` (SKIP ≠ PASS);
* ``item.evidence_path`` points at the file that was actually written (the
  ``safe_id`` spelling), unlike the reporter's copy.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from forgeflow.qa.analyzer import FailureClassifier
from forgeflow.qa.config import Profile, profile_targets
from forgeflow.qa.evidence import EvidenceStore, safe_id
from forgeflow.qa.executor import TestExecutor
from forgeflow.qa.models import (
    CapabilityInventory,
    EvidenceStatus,
    Layer,
    ProcessResult,
    ProcessStatus,
    TaskResult,
    TaskSpec,
    TestPlanItem,
    TestStatus,
    TestType,
)
from forgeflow.qa.planner import TestPlanner

# ── shared stubs / helpers ───────────────────────────────────────────────────


class _StubTasks:
    """A TaskRunnerAdapter stub that records what it was asked to run."""

    def __init__(self, *, process: ProcessResult | None = None, exists: bool = True) -> None:
        self._process = process
        self._exists = exists
        self.calls: list[str] = []

    def has_target(self, target: str) -> bool:
        return self._exists

    def command_for(self, target: str) -> list[str]:
        return ["make", "PY=fake", target]

    def discover_tasks(self) -> list[TaskSpec]:
        return []

    def get_result(self, target: str) -> TaskResult | None:
        return None

    def run_task(self, task: TaskSpec, *, run_id: str) -> TaskResult:
        self.calls.append(task.target)
        assert self._process is not None  # only reached when a run is expected
        return TaskResult(
            target=task.target, ok=self._process.status is ProcessStatus.OK, result=self._process
        )


def _inventory() -> CapabilityInventory:
    return CapabilityInventory(
        run_id="r",
        generated_at=datetime.now(UTC).isoformat(),
        root="/x",
        layers={layer: [] for layer in Layer},
        totals={"_all": 0},
    )


def _plan_item(
    test_id: str = "make:check",
    *,
    status: TestStatus = TestStatus.PENDING,
    target: str | None = "check",
) -> TestPlanItem:
    return TestPlanItem(
        test_id=test_id,
        capability_id=f"BUILD:{target}",
        layer=Layer.BUILD,
        test_type=TestType.STATIC,
        description="d",
        task_target=target,
        command=["make", "PY=fake", target or ""],
        status=status,
    )


def _process(
    status: ProcessStatus,
    exit_code: int | None,
    *,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
) -> ProcessResult:
    return ProcessResult(
        command=["make", "check"],
        cwd="/x",
        env_diff={},
        exit_code=exit_code,
        status=status,
        stdout=stdout,
        stderr=stderr,
        duration_ms=5,
        started_at=datetime.now(UTC).isoformat(),
        timed_out=timed_out,
        timeout_s=10,
    )


def _executor(tmp_path: Path, tasks: _StubTasks) -> TestExecutor:
    from forgeflow.qa.config import QaRunConfig

    cfg = QaRunConfig(project_root=tmp_path, artifacts_dir=tmp_path / "run", run_id="r")
    store = EvidenceStore.for_run(cfg)
    return TestExecutor(object(), tasks, store, FailureClassifier())


# ── planner ──────────────────────────────────────────────────────────────────


def test_planner_fast_and_full_target_selection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("QA_MAKE_TARGETS", raising=False)
    planner = TestPlanner(tmp_path, _StubTasks(exists=True))
    fast_ids = [item.test_id for item in planner.plan(_inventory(), profile=Profile.FAST)]
    full_ids = [item.test_id for item in planner.plan(_inventory(), profile=Profile.FULL)]
    assert fast_ids == [f"make:{t}" for t in profile_targets(Profile.FAST)] == ["make:check"]
    assert full_ids == [f"make:{t}" for t in profile_targets(Profile.FULL)]
    assert "make:test-integration" in full_ids


def test_planner_marks_missing_target_inconclusive_and_empty_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("QA_MAKE_TARGETS", "totally-not-a-target")
    planner = TestPlanner(tmp_path, _StubTasks(exists=False))
    items = planner.plan(_inventory(), profile=Profile.FAST)
    assert len(items) == 1
    item = items[0]
    assert item.status is TestStatus.INCONCLUSIVE
    assert item.command == []  # no fabricated argv for a target that cannot run
    assert item.task_target == "totally-not-a-target"


# ── executor: status derivation ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "exit_code", "stdout", "expected"),
    [
        (ProcessStatus.OK, 0, "1 passed in 0.10s", EvidenceStatus.PASS),
        (ProcessStatus.OK, 0, "no tests ran in 0.01s", EvidenceStatus.INCONCLUSIVE),
        (ProcessStatus.OK, 0, "collected 0 items", EvidenceStatus.INCONCLUSIVE),
        (ProcessStatus.FAILED, 1, "1 failed", EvidenceStatus.FAIL),
        (ProcessStatus.SPAWN_ERROR, None, "", EvidenceStatus.BLOCKED),
        (ProcessStatus.TIMEOUT, None, "", EvidenceStatus.FAIL),
    ],
    # Explicit ids so the notice phrases ("no tests ran" / "collected 0 items")
    # are never echoed into this suite's verbose output — that leak is exactly
    # what let an unrelated passing id drift a failure's attribution.
    ids=[
        "ok_pass_summary",
        "ok_notice_no_tests_ran",
        "ok_notice_collected_zero",
        "failed_exit_1",
        "spawn_error",
        "timeout",
    ],
)
def test_executor_status_matrix(
    tmp_path: Path,
    status: ProcessStatus,
    exit_code: int | None,
    stdout: str,
    expected: EvidenceStatus,
) -> None:
    tasks = _StubTasks(process=_process(status, exit_code, stdout=stdout))
    evidence = _executor(tmp_path, tasks).execute([_plan_item()], run_id="r")
    assert evidence[0].status is expected
    assert tasks.calls == ["check"]  # it really ran


def test_executor_never_passes_when_nothing_collected(tmp_path: Path) -> None:
    tasks = _StubTasks(process=_process(ProcessStatus.OK, 0, stdout="no tests ran"))
    evidence = _executor(tmp_path, tasks).execute([_plan_item()], run_id="r")
    assert evidence[0].status is not EvidenceStatus.PASS


def test_executor_skips_unrealisable_item_without_running(tmp_path: Path) -> None:
    tasks = _StubTasks(process=_process(ProcessStatus.OK, 0))
    item = _plan_item(status=TestStatus.INCONCLUSIVE)
    evidence = _executor(tmp_path, tasks).execute([item], run_id="r")
    assert evidence[0].status is EvidenceStatus.INCONCLUSIVE
    assert evidence[0].exit_code is None
    assert tasks.calls == []  # never executed


def test_executor_skips_none_target_without_running(tmp_path: Path) -> None:
    tasks = _StubTasks(process=_process(ProcessStatus.OK, 0))
    item = _plan_item(target=None)
    evidence = _executor(tmp_path, tasks).execute([item], run_id="r")
    assert evidence[0].status is EvidenceStatus.INCONCLUSIVE
    assert tasks.calls == []


def test_executor_evidence_path_matches_the_written_file(tmp_path: Path) -> None:
    tasks = _StubTasks(process=_process(ProcessStatus.OK, 0, stdout="1 passed"))
    item = _plan_item(test_id="make:check")
    executor = _executor(tmp_path, tasks)
    executor.execute([item], run_id="r")
    expected = f"evidence/{safe_id('make:check')}.json"
    assert item.evidence_path == expected
    assert (executor._store.run_dir / expected).is_file()
