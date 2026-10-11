"""Adversarial honesty probes for the Phase A–C QA core.

These tests *try to falsify* the invariants the whole sub-package exists to
protect. Each one is written so that a regression that reintroduced "fake green"
would flip it red:

* **SKIP ≠ PASS** — a run with no real passing item is never ``PASS``.
* **Evidence is immutable** — a reused ``run_id``/artifact path is refused.
* **No signature ⇒ no blame** — the classifier never emits ``SOURCE_BUG`` for an
  ambiguous failure.
* **Unplannable ⇒ never executed, never PASS** — a target that is not in the
  Makefile is planned ``INCONCLUSIVE`` and the runner is never even called.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from forgeflow.qa.analyzer import FailureClassifier
from forgeflow.qa.config import Profile, QaRunConfig
from forgeflow.qa.evidence import EvidenceStore
from forgeflow.qa.executor import TestExecutor
from forgeflow.qa.models import (
    CapabilityInventory,
    Evidence,
    EvidenceStatus,
    FailureClass,
    Layer,
    ProcessResult,
    ProcessStatus,
    TaskResult,
    TaskSpec,
    TestPlanItem,
    TestStatus,
    TestType,
    Verdict,
)
from forgeflow.qa.planner import TestPlanner
from forgeflow.qa.reporter import Reporter

# ── shared helpers ───────────────────────────────────────────────────────────


def _evidence(test_id: str, status: EvidenceStatus) -> Evidence:
    return Evidence(
        evidence_id=f"r:{test_id}",
        run_id="r",
        capability_id="BUILD:check",
        test_id=test_id,
        command=["make", "PY=x", "check"],
        environment={"cwd": "/x"},
        exit_code=0,
        status=status,
        timestamp=datetime.now(UTC).isoformat(),
    )


def _process(status: ProcessStatus, exit_code: int | None, stdout: str = "") -> ProcessResult:
    return ProcessResult(
        command=["make", "check"],
        cwd="/x",
        env_diff={},
        exit_code=exit_code,
        status=status,
        stdout=stdout,
        stderr="",
        duration_ms=1,
        started_at=datetime.now(UTC).isoformat(),
        timeout_s=10,
    )


class _TripwireTasks:
    """A TaskRunnerAdapter that fails loudly if it is ever asked to run."""

    def has_target(self, target: str) -> bool:
        return False

    def command_for(self, target: str) -> list[str]:
        return []

    def discover_tasks(self) -> list[TaskSpec]:
        return []

    def get_result(self, target: str) -> TaskResult | None:
        return None

    def run_task(self, task: TaskSpec, *, run_id: str) -> TaskResult:
        raise AssertionError(f"BUG: unplannable target {task.target!r} was executed")


def _inventory() -> CapabilityInventory:
    return CapabilityInventory(
        run_id="r",
        generated_at=datetime.now(UTC).isoformat(),
        root="/x",
        layers={layer: [] for layer in Layer},
        totals={"_all": 0},
    )


# ── probe 1: SKIP ≠ PASS ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "statuses",
    [
        [EvidenceStatus.SKIP],
        [EvidenceStatus.INCONCLUSIVE],
        [EvidenceStatus.SKIP, EvidenceStatus.INCONCLUSIVE],
        [EvidenceStatus.SKIP, EvidenceStatus.SKIP],
    ],
)
def test_no_real_pass_never_yields_pass(tmp_path: Path, statuses: list[EvidenceStatus]) -> None:
    evidence = [_evidence(f"t{i}", status) for i, status in enumerate(statuses)]
    verdict = Reporter(EvidenceStore(tmp_path / "run")).verdict(evidence)
    assert verdict is not Verdict.PASS
    assert verdict is Verdict.INCONCLUSIVE


def test_empty_evidence_is_inconclusive(tmp_path: Path) -> None:
    assert Reporter(EvidenceStore(tmp_path / "run")).verdict([]) is Verdict.INCONCLUSIVE


# ── probe 2: evidence immutability ───────────────────────────────────────────


def test_run_directory_cannot_be_reused(tmp_path: Path) -> None:
    cfg = QaRunConfig(project_root=tmp_path, artifacts_dir=tmp_path / "run", run_id="r")
    EvidenceStore.for_run(cfg)
    with pytest.raises(RuntimeError):
        EvidenceStore.for_run(cfg)


def test_artifact_cannot_be_overwritten(tmp_path: Path) -> None:
    cfg = QaRunConfig(project_root=tmp_path, artifacts_dir=tmp_path / "run", run_id="r")
    store = EvidenceStore.for_run(cfg)
    store.save_evidence(_evidence("make:check", EvidenceStatus.PASS))
    # A second save of the same test id maps to the same file and must be refused.
    with pytest.raises(FileExistsError):
        store.save_evidence(_evidence("make:check", EvidenceStatus.FAIL))


# ── probe 3: no signature ⇒ no blame ─────────────────────────────────────────


@pytest.mark.parametrize(
    "noise",
    [
        "1 failed, 1 passed",
        "AssertionError",
        "exit code 1",
        "random gibberish 0xdeadbeef",
        "FAILED (failures=3)",
    ],
    ids=[
        "probe_failed_passed",
        "probe_assertion",
        "probe_exit_code",
        "probe_gibberish",
        "probe_failed_prefix",
    ],
)
def test_classifier_never_invents_a_source_bug(noise: str) -> None:
    item = TestPlanItem(
        test_id="t",
        capability_id="c",
        layer=Layer.BUILD,
        test_type=TestType.UNIT,
        description="d",
    )
    result = _process(ProcessStatus.FAILED, 1, stdout=noise)
    got = FailureClassifier().classify(result, item, env_findings=[])
    assert got is not FailureClass.SOURCE_BUG


# ── probe 4: unplannable ⇒ never executed, never PASS ────────────────────────


def test_unplannable_target_is_never_executed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("QA_MAKE_TARGETS", "ghost-target")
    planner = TestPlanner(tmp_path, _TripwireTasks())
    plan = planner.plan(_inventory(), profile=Profile.FAST)
    assert [item.status for item in plan] == [TestStatus.INCONCLUSIVE]

    cfg = QaRunConfig(project_root=tmp_path, artifacts_dir=tmp_path / "run", run_id="r")
    store = EvidenceStore.for_run(cfg)
    executor = TestExecutor(object(), _TripwireTasks(), store, FailureClassifier())
    evidence = executor.execute(plan, run_id="r")  # must not touch the tripwire
    assert [record.status for record in evidence] == [EvidenceStatus.INCONCLUSIVE]

    verdict = Reporter(store).verdict(evidence)
    assert verdict is Verdict.INCONCLUSIVE
    assert verdict is not Verdict.PASS


def test_exit_zero_but_nothing_collected_is_not_a_pass(tmp_path: Path) -> None:
    class _NothingRan:
        def has_target(self, target: str) -> bool:
            return True

        def command_for(self, target: str) -> list[str]:
            return ["make", "check"]

        def discover_tasks(self) -> list[TaskSpec]:
            return []

        def get_result(self, target: str) -> TaskResult | None:
            return None

        def run_task(self, task: TaskSpec, *, run_id: str) -> TaskResult:
            process = _process(ProcessStatus.OK, 0, stdout="no tests ran in 0.01s")
            return TaskResult(target=task.target, ok=True, result=process)

    item = TestPlanItem(
        test_id="make:check",
        capability_id="BUILD:check",
        layer=Layer.BUILD,
        test_type=TestType.STATIC,
        description="d",
        task_target="check",
        command=["make", "check"],
    )
    cfg = QaRunConfig(project_root=tmp_path, artifacts_dir=tmp_path / "run", run_id="r")
    store = EvidenceStore.for_run(cfg)
    executor = TestExecutor(object(), _NothingRan(), store, FailureClassifier())
    evidence = executor.execute([item], run_id="r")
    assert evidence[0].status is EvidenceStatus.INCONCLUSIVE
    assert Reporter(store).verdict(evidence) is Verdict.INCONCLUSIVE
