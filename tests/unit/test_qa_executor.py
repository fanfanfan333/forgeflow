"""Unit tests for :mod:`forgeflow.qa.executor`."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from forgeflow.qa.analyzer import FailureClassifier
from forgeflow.qa.config import QaRunConfig
from forgeflow.qa.evidence import EvidenceStore
from forgeflow.qa.executor import TestExecutor
from forgeflow.qa.models import (
    EvidenceStatus,
    Layer,
    ProcessResult,
    ProcessStatus,
    TestPlanItem,
    TestStatus,
    TestType,
)
from forgeflow.qa.paths import find_project_root
from forgeflow.qa.process_runner import ProcessRunner
from forgeflow.qa.task_runner import MakeTaskRunnerAdapter


def _proc(exit_code: int, status: ProcessStatus) -> ProcessResult:
    return ProcessResult(
        command=["make", "PY=PYX", "check"],
        cwd="/x",
        env_diff={"LLM_PROVIDER": "mock"},
        exit_code=exit_code,
        status=status,
        stdout="out",
        stderr="err",
        duration_ms=7,
        started_at="2026-01-01T00:00:00+00:00",
        timed_out=False,
        timeout_s=10,
    )


class _CannedRunner:
    def __init__(self, result: ProcessResult) -> None:
        self.result = result
        self.calls: list[list[str]] = []

    def run(self, command, *, cwd=None, timeout_s=None, env=None):
        self.calls.append(command)
        return self.result


def _item(*, status: TestStatus = TestStatus.PENDING, target: str | None = "check") -> TestPlanItem:
    return TestPlanItem(
        test_id=f"make:{target}",
        capability_id=f"BUILD:{target}",
        layer=Layer.BUILD,
        test_type=TestType.STATIC,
        description="d",
        task_target=target,
        command=["make-x", "PY=PYX", target] if target else [],
        status=status,
    )


def _executor(tmp_path, runner) -> tuple[TestExecutor, EvidenceStore]:
    config = QaRunConfig(
        project_root=find_project_root(),
        artifacts_dir=tmp_path / "run",
        run_id="r",
    )
    store = EvidenceStore.for_run(config)
    adapter = MakeTaskRunnerAdapter(
        find_project_root() / "Makefile", "PYX", runner, make_exe="make-x"
    )
    executor = TestExecutor(runner, adapter, store, FailureClassifier())
    return executor, store


def test_passing_run_records_pass_and_artifacts(tmp_path):
    runner = _CannedRunner(_proc(0, ProcessStatus.OK))
    executor, store = _executor(tmp_path, runner)
    evidence = executor.execute([_item()], run_id="r")
    assert evidence[0].status is EvidenceStatus.PASS
    assert evidence[0].exit_code == 0
    assert evidence[0].duration_ms == 7
    assert evidence[0].stdout_artifact == "artifacts/make_check.stdout.txt"
    assert (store.run_dir / "artifacts" / "make_check.stdout.txt").read_text() == "out"
    assert (store.run_dir / "artifacts" / "make_check.stderr.txt").read_text() == "err"
    assert (store.run_dir / "evidence" / "make_check.json").is_file()
    assert runner.calls[-1] == ["make-x", "PY=PYX", "check"]


def test_failing_run_is_classified(tmp_path):
    runner = _CannedRunner(_proc(1, ProcessStatus.FAILED))
    executor, store = _executor(tmp_path, runner)
    evidence = executor.execute([_item()], run_id="r")
    assert evidence[0].status is EvidenceStatus.FAIL
    assert evidence[0].failure_class is not None
    assert store.list_evidence()[0].status is EvidenceStatus.FAIL


def test_inconclusive_item_is_not_run(tmp_path):
    runner = _CannedRunner(_proc(0, ProcessStatus.OK))
    executor, store = _executor(tmp_path, runner)
    item = _item(status=TestStatus.INCONCLUSIVE, target=None)
    evidence = executor.execute([item], run_id="r")
    assert evidence[0].status is EvidenceStatus.INCONCLUSIVE
    assert evidence[0].exit_code is None
    assert runner.calls == []  # nothing was run
    assert (store.run_dir / "evidence" / "make_None.json").is_file()


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


@pytest.mark.skipif(_resolve_make() is None, reason="make is not available")
def test_executor_runs_a_real_cheap_target(tmp_path):
    root = find_project_root()
    config = QaRunConfig(project_root=root, artifacts_dir=tmp_path / "run", run_id="r")
    store = EvidenceStore.for_run(config)
    adapter = MakeTaskRunnerAdapter(
        root / "Makefile",
        sys.executable,
        ProcessRunner(default_timeout_s=60),
        make_exe=_resolve_make(),
    )
    executor = TestExecutor(None, adapter, store, FailureClassifier())
    item = TestPlanItem(
        test_id="make:help",
        capability_id="BUILD:help",
        layer=Layer.BUILD,
        test_type=TestType.STATIC,
        description="make help",
        task_target="help",
        command=adapter.command_for("help"),
    )
    evidence = executor.execute([item], run_id="r")
    assert evidence[0].status is EvidenceStatus.PASS
    assert evidence[0].exit_code == 0
