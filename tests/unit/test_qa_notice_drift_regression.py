"""Regression pin for the *notice-drift* defect (analyzer collection family).

Round-2 independently reproduced a residual: the classifier's collection family
still substring-matched ``no tests ran`` / ``collected 0 items``, so a real
assertion failure whose transcript incidentally contained an **unrelated PASSING
parametrised id** echoing those words was mislabeled ``TEST_BUG`` (it should be
``UNKNOWN``). The engineer routed both :mod:`forgeflow.qa.executor` and
:mod:`forgeflow.qa.analyzer` through the line-anchored
:mod:`forgeflow.qa.text_signals`; this file pins that fix end-to-end.

Design notes
------------
* The transcript is assembled from module-level **literals**, so the phrases are
  never echoed into this suite's own verbose ids/logs — that leak is precisely
  what the defect exploited.
* Assertions are strong (``is FailureClass.UNKNOWN``), offline and deterministic;
  nothing here depends on the realtime verbose output of another test.
* One case pins the opposite direction too (a *genuine* collection notice must
  still be ``TEST_BUG``) so the pin cannot be satisfied by simply never matching.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from forgeflow.qa.analyzer import FailureClassifier
from forgeflow.qa.config import QaRunConfig
from forgeflow.qa.evidence import EvidenceStore
from forgeflow.qa.executor import TestExecutor, _ran_nothing
from forgeflow.qa.models import (
    EvidenceStatus,
    FailureClass,
    Layer,
    ProcessResult,
    ProcessStatus,
    TaskResult,
    TaskSpec,
    TestPlanItem,
    TestType,
)

#: Two unrelated PASSING parametrised ids that merely *echo* the notice phrases.
_INCIDENTAL_PASS_LINES = (
    "tests/unit/test_qa_plan_exec_edges.py::test_executor_status_matrix"
    "[ok-0-no tests ran in 0.01s-INCONCLUSIVE] PASSED [ 73%]\n"
    "tests/unit/test_qa_plan_exec_edges.py::test_executor_status_matrix"
    "[ok-0-collected 0 items-INCONCLUSIVE] PASSED [ 73%]\n"
)

#: A minimal, realistic pytest failure summary with no real "nothing ran" event.
_REAL_ASSERTION_FAILURE = (
    "=========================== short test summary info ===========================\n"
    "FAILED tests/unit/test_widget.py::test_widget_spins - AssertionError: assert 1 == 2\n"
    "==================== 1 failed, 3573 passed in 200.00s ====================\n"
)


def _item(test_id: str = "make:check", target: str | None = "check") -> TestPlanItem:
    return TestPlanItem(
        test_id=test_id,
        capability_id=f"BUILD:{target}",
        layer=Layer.BUILD,
        test_type=TestType.STATIC,
        description="d",
        task_target=target,
        command=["make", target] if target else [],
    )


def _proc(
    status: ProcessStatus,
    exit_code: int | None,
    *,
    stdout: str = "",
    stderr: str = "",
) -> ProcessResult:
    return ProcessResult(
        command=["make", "check"],
        cwd="/x",
        env_diff={},
        exit_code=exit_code,
        status=status,
        stdout=stdout,
        stderr=stderr,
        duration_ms=1,
        started_at=datetime.now(UTC).isoformat(),
        timeout_s=10,
    )


# ── the defect: incidental phrases must not decide attribution ───────────────


def test_incidental_passing_ids_do_not_make_a_real_failure_a_test_bug() -> None:
    transcript = _INCIDENTAL_PASS_LINES + _REAL_ASSERTION_FAILURE
    got = FailureClassifier().classify(
        _proc(ProcessStatus.FAILED, 1, stdout=transcript), _item(), env_findings=[]
    )
    # The ONLY "collection-looking" text is an unrelated PASSING id, so no family
    # is honestly recognisable → UNKNOWN. TEST_BUG would be the old drift.
    assert got is not FailureClass.TEST_BUG
    assert got is FailureClass.UNKNOWN


def test_incidental_phrases_in_stderr_do_not_relabel_either() -> None:
    got = FailureClassifier().classify(
        _proc(
            ProcessStatus.FAILED,
            1,
            stdout="FAILED tests/x.py::test_y - AssertionError\n",
            stderr=_INCIDENTAL_PASS_LINES,
        ),
        _item(),
        env_findings=[],
    )
    assert got is FailureClass.UNKNOWN


# ── the opposite direction: genuine notices are still detected ───────────────


def test_genuine_nothing_ran_notice_is_still_a_collection_test_bug() -> None:
    transcript = "===== collected 0 items =====\n===== 0.00s =====\n"
    got = FailureClassifier().classify(
        _proc(ProcessStatus.FAILED, 1, stdout=transcript), _item(), env_findings=[]
    )
    assert got is FailureClass.TEST_BUG


def test_genuine_collection_error_notice_is_still_a_collection_test_bug() -> None:
    transcript = "collected 0 items / 1 error\n"
    got = FailureClassifier().classify(
        _proc(ProcessStatus.FAILED, 1, stdout=transcript), _item(), env_findings=[]
    )
    assert got is FailureClass.TEST_BUG


# ── executor notice detection is line-anchored, both ways ────────────────────


def test_executor_notice_detection_is_line_anchored_both_ways() -> None:
    assert _ran_nothing(_INCIDENTAL_PASS_LINES) is False
    assert _ran_nothing("no tests ran in 0.01s") is True
    assert _ran_nothing("====== no tests ran in 0.01s ======") is True
    assert _ran_nothing("collected 0 items") is True
    assert _ran_nothing("collected 0 items / 1 error") is True
    assert _ran_nothing("1 passed in 0.10s") is False


# ── a collection error with a non-zero exit is FAIL, never INCONCLUSIVE ──────


class _StubTasks:
    """Runs ``check`` and returns a canned non-zero process result."""

    def __init__(self, process: ProcessResult) -> None:
        self._process = process

    def has_target(self, target: str) -> bool:
        return True

    def command_for(self, target: str) -> list[str]:
        return ["make", target]

    def discover_tasks(self) -> list[TaskSpec]:
        return []

    def get_result(self, target: str) -> TaskResult | None:
        return None

    def run_task(self, task: TaskSpec, *, run_id: str) -> TaskResult:
        return TaskResult(target=task.target, ok=False, result=self._process)


def test_collection_error_with_nonzero_exit_is_fail_not_inconclusive(tmp_path: Path) -> None:
    # "collected 0 items / 1 error" IS a notice line, but pytest exits non-zero
    # for a collection error: the executor must report FAIL, never INCONCLUSIVE.
    process = _proc(ProcessStatus.FAILED, 2, stdout="collected 0 items / 1 error\n")
    cfg = QaRunConfig(project_root=tmp_path, artifacts_dir=tmp_path / "run", run_id="r")
    store = EvidenceStore.for_run(cfg)
    executor = TestExecutor(object(), _StubTasks(process), store, FailureClassifier())
    evidence = executor.execute([_item()], run_id="r")
    assert evidence[0].status is EvidenceStatus.FAIL
    assert evidence[0].status is not EvidenceStatus.INCONCLUSIVE


def test_exit_zero_with_a_genuine_notice_is_inconclusive(tmp_path: Path) -> None:
    # The INCONCLUSIVE branch is reserved for exit-0 runs that executed nothing.
    process = _proc(ProcessStatus.OK, 0, stdout="===== no tests ran in 0.01s =====\n")
    cfg = QaRunConfig(project_root=tmp_path, artifacts_dir=tmp_path / "run", run_id="r")
    store = EvidenceStore.for_run(cfg)
    executor = TestExecutor(object(), _StubTasks(process), store, FailureClassifier())
    evidence = executor.execute([_item()], run_id="r")
    assert evidence[0].status is EvidenceStatus.INCONCLUSIVE
