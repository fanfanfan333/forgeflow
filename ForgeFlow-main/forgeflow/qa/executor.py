"""Test executor — run the plan, persist evidence (Phase C, §79/§80/§44).

Layer L4. For every :class:`TestPlanItem` it runs the real Make target through
the task adapter, turns the :class:`ProcessResult` into an :class:`Evidence`
record, and writes both the raw ``stdout``/``stderr`` artifacts and the evidence
JSON into the run directory.

Honesty rules enforced here:

* An item that could not be planned (missing target) is recorded ``INCONCLUSIVE``
  — it is never run, and never marked ``PASS`` (SKIP ≠ PASS).
* The evidence ``status`` is derived from the *real* exit code / process status.
* A failed run's ``failure_class`` comes from the classifier, which refuses to
  blame the source without a signature.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from forgeflow.qa.analyzer import FailureClassifier
from forgeflow.qa.environment_guard import EnvFinding
from forgeflow.qa.evidence import EvidenceStore, safe_id
from forgeflow.qa.models import (
    Evidence,
    EvidenceStatus,
    FailureClass,
    ProcessResult,
    ProcessStatus,
    TaskSpec,
    TestPlanItem,
    TestStatus,
)
from forgeflow.qa.task_runner import TaskRunnerAdapter
from forgeflow.qa.text_signals import NOTHING_RAN_PHRASES, output_has_notice

logger = logging.getLogger(__name__)

_EVIDENCE_TO_TEST_STATUS: dict[EvidenceStatus, TestStatus] = {
    EvidenceStatus.PASS: TestStatus.PASS,
    EvidenceStatus.FAIL: TestStatus.FAIL,
    EvidenceStatus.BLOCKED: TestStatus.BLOCKED,
    EvidenceStatus.INCONCLUSIVE: TestStatus.INCONCLUSIVE,
    EvidenceStatus.SKIP: TestStatus.INCONCLUSIVE,
}


def _ran_nothing(output: str) -> bool:
    """True iff ``output`` reports that the run executed no tests at all.

    The line-anchored detection is shared with the classifier via
    :mod:`forgeflow.qa.text_signals`, so the executor and :mod:`.analyzer` can
    never drift apart again. Aggregate targets (``check``) emit the whole
    suite's verbose output, so a descriptive test id or log line containing the
    words (``…status_matrix[ok-0-no tests ran in 0.01s-…]``) is *not* mistaken
    for "the run executed nothing".
    """
    return output_has_notice(output, NOTHING_RAN_PHRASES)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class TestExecutor:
    """Executes a plan and records immutable evidence for each item."""

    __test__ = False  # not a pytest test class despite the ``Test`` name

    def __init__(
        self,
        runner: object,
        tasks: TaskRunnerAdapter,
        store: EvidenceStore,
        classifier: FailureClassifier,
    ) -> None:
        self._tasks = tasks
        self._store = store
        self._classifier = classifier
        # ``runner`` is accepted for symmetry with the design's constructor
        # signature; the Make adapter already owns the process runner it uses.

    def execute(
        self,
        plan: list[TestPlanItem],
        *,
        run_id: str,
        env_findings: list[EnvFinding] | None = None,
    ) -> list[Evidence]:
        """Run every planned item and return the ordered evidence list."""
        findings = env_findings or []
        evidence: list[Evidence] = []
        for item in plan:
            evidence.append(self._execute_item(item, run_id=run_id, env_findings=findings))
        return evidence

    # ── internals ────────────────────────────────────────────────────────
    def _status_from_process(self, process: ProcessResult) -> EvidenceStatus:
        """Derive an honest evidence status from a real process outcome."""
        if process.status is ProcessStatus.SPAWN_ERROR:
            return EvidenceStatus.BLOCKED
        if process.status is ProcessStatus.TIMEOUT:
            return EvidenceStatus.FAIL
        if process.exit_code == 0:
            combined = f"{process.stdout}\n{process.stderr}"
            if _ran_nothing(combined):
                return EvidenceStatus.INCONCLUSIVE
            return EvidenceStatus.PASS
        return EvidenceStatus.FAIL

    def _record(
        self,
        item: TestPlanItem,
        *,
        run_id: str,
        status: EvidenceStatus,
        exit_code: int | None,
        environment: dict[str, Any],
        duration_ms: int,
        stdout_artifact: str | None,
        stderr_artifact: str | None,
        failure_class: FailureClass | None,
        timestamp: str,
    ) -> Evidence:
        evidence = Evidence(
            evidence_id=f"{run_id}:{item.test_id}",
            run_id=run_id,
            capability_id=item.capability_id,
            test_id=item.test_id,
            command=item.command,
            environment=environment,
            exit_code=exit_code,
            status=status,
            stdout_artifact=stdout_artifact,
            stderr_artifact=stderr_artifact,
            attachments=[],
            timestamp=timestamp,
            duration_ms=duration_ms,
            failure_class=failure_class,
        )
        self._store.save_evidence(evidence)
        item.status = _EVIDENCE_TO_TEST_STATUS[status]
        item.failure_class = failure_class
        item.evidence_path = f"evidence/{safe_id(item.test_id)}.json"
        return evidence

    def _execute_item(
        self,
        item: TestPlanItem,
        *,
        run_id: str,
        env_findings: list[EnvFinding],
    ) -> Evidence:
        timestamp = _utc_now_iso()

        # A target that could not be planned is honestly INCONCLUSIVE — never run,
        # never PASS (SKIP ≠ PASS).
        if item.status is TestStatus.INCONCLUSIVE or item.task_target is None:
            logger.warning("executor: skipping unrealisable item %s", item.test_id)
            return self._record(
                item,
                run_id=run_id,
                status=EvidenceStatus.INCONCLUSIVE,
                exit_code=None,
                environment={"reason": "target not available", "run_id": run_id},
                duration_ms=0,
                stdout_artifact=None,
                stderr_artifact=None,
                failure_class=None,
                timestamp=timestamp,
            )

        item.status = TestStatus.RUNNING
        spec = TaskSpec(target=item.task_target, description=item.description, kind="make")
        task_result = self._tasks.run_task(spec, run_id=run_id)
        process = task_result.result

        status = self._status_from_process(process)
        failure_class = None
        if status is not EvidenceStatus.PASS:
            failure_class = self._classifier.classify(process, item, env_findings=env_findings)

        stem = safe_id(item.test_id)
        stdout_rel = f"artifacts/{stem}.stdout.txt"
        stderr_rel = f"artifacts/{stem}.stderr.txt"
        self._store.write_bytes(stdout_rel, process.stdout.encode("utf-8"))
        self._store.write_bytes(stderr_rel, process.stderr.encode("utf-8"))

        environment = {
            "cwd": process.cwd,
            "env_diff": process.env_diff,
            "timeout_s": process.timeout_s,
            "started_at": process.started_at,
        }
        return self._record(
            item,
            run_id=run_id,
            status=status,
            exit_code=process.exit_code,
            environment=environment,
            duration_ms=process.duration_ms,
            stdout_artifact=stdout_rel,
            stderr_artifact=stderr_rel,
            failure_class=failure_class,
            timestamp=timestamp,
        )


__all__ = ["TestExecutor"]
