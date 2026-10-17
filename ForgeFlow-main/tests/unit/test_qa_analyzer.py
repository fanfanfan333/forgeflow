"""Unit tests for :mod:`forgeflow.qa.analyzer` (14-class coverage)."""

from __future__ import annotations

from forgeflow.qa.analyzer import FailureClassifier
from forgeflow.qa.environment_guard import EnvFinding
from forgeflow.qa.models import (
    Evidence,
    EvidenceStatus,
    FailureClass,
    Layer,
    ProcessResult,
    ProcessStatus,
    TestPlanItem,
    TestType,
)


def _item(*, requires=None, test_id="make:check") -> TestPlanItem:
    return TestPlanItem(
        test_id=test_id,
        capability_id="BUILD:check",
        layer=Layer.BUILD,
        test_type=TestType.STATIC,
        description="d",
        task_target="check",
        requires=list(requires or []),
    )


def _proc(
    *,
    status: ProcessStatus = ProcessStatus.FAILED,
    exit_code: int | None = 1,
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
        duration_ms=1,
        started_at="2026-01-01T00:00:00+00:00",
        timed_out=timed_out,
        timeout_s=10,
    )


def _classify(proc, item=None, findings=None):
    return FailureClassifier().classify(proc, item or _item(), env_findings=findings or [])


def test_blocking_env_finding_wins():
    finding = EnvFinding(check="wrong_python", ok=False, detail="stub", severity="blocking")
    assert _classify(_proc(), findings=[finding]) is FailureClass.ENVIRONMENT_FAILURE


def test_timeout_without_requirements_is_tool_failure():
    proc = _proc(status=ProcessStatus.TIMEOUT, exit_code=None, timed_out=True)
    assert _classify(proc) is FailureClass.TOOL_FAILURE


def test_timeout_with_requirements_is_environment_failure():
    proc = _proc(status=ProcessStatus.TIMEOUT, exit_code=None, timed_out=True)
    assert _classify(proc, _item(requires=["pg"])) is FailureClass.ENVIRONMENT_FAILURE


def test_spawn_error_is_tool_failure():
    proc = _proc(status=ProcessStatus.SPAWN_ERROR, exit_code=None)
    assert _classify(proc) is FailureClass.TOOL_FAILURE


def test_missing_module_is_dependency_failure():
    proc = _proc(stderr="ModuleNotFoundError: No module named 'foo'")
    assert _classify(proc) is FailureClass.DEPENDENCY_FAILURE


def test_connection_refused_to_database_is_database_failure():
    proc = _proc(stderr="asyncpg: connection refused to postgres")
    assert _classify(proc) is FailureClass.DATABASE_FAILURE


def test_connection_refused_generic_is_environment_failure():
    proc = _proc(stderr="httpx: connection refused")
    assert _classify(proc) is FailureClass.ENVIRONMENT_FAILURE


def test_alembic_is_migration_failure():
    proc = _proc(stdout="FAILED: alembic upgrade head - target database is not up to date")
    assert _classify(proc) is FailureClass.MIGRATION_FAILURE


def test_no_tests_ran_is_test_bug():
    proc = _proc(stdout="no tests ran in 0.01s")
    assert _classify(proc) is FailureClass.TEST_BUG


def test_source_syntax_error_is_source_bug():
    proc = _proc(stderr='  File "forgeflow/service.py", line 3\n    SyntaxError: invalid syntax')
    assert _classify(proc) is FailureClass.SOURCE_BUG


def test_test_file_syntax_error_is_test_bug():
    proc = _proc(stderr='  File "tests/unit/test_x.py", line 3\n    SyntaxError: bad')
    assert _classify(proc) is FailureClass.TEST_BUG


def test_permission_denied_is_permission_failure():
    proc = _proc(stdout="HTTP 403 Forbidden")
    assert _classify(proc) is FailureClass.PERMISSION_FAILURE


def test_generic_test_failure_is_not_source_bug():
    # The critical counterexample: "1 failed" must NOT be blamed on the source.
    proc = _proc(stdout="================ 1 failed, 4 passed in 2.0s ================")
    result = _classify(proc)
    assert result is FailureClass.UNKNOWN
    assert result is not FailureClass.SOURCE_BUG


def test_relevant_service_finding_routes_to_environment():
    finding = EnvFinding(
        check="service_unavailable", ok=False, detail="postgres unreachable", severity="warning"
    )
    proc = _proc(stderr="ConnectionError: could not connect")
    assert (
        _classify(proc, _item(requires=["pg"]), findings=[finding])
        is FailureClass.ENVIRONMENT_FAILURE
    )


def test_analyze_aggregates_all_14_classes():
    evidence = [
        Evidence(
            evidence_id="r:t1",
            run_id="r",
            capability_id="c",
            test_id="t1",
            command=["x"],
            exit_code=1,
            status=EvidenceStatus.FAIL,
            timestamp="2026-01-01T00:00:00+00:00",
            failure_class=FailureClass.UNKNOWN,
        ),
        Evidence(
            evidence_id="r:t2",
            run_id="r",
            capability_id="c",
            test_id="t2",
            command=["x"],
            exit_code=0,
            status=EvidenceStatus.PASS,
            timestamp="2026-01-01T00:00:00+00:00",
        ),
    ]
    analysis = FailureClassifier().analyze(evidence)
    assert analysis["total"] == 2
    assert set(analysis["by_failure_class"]) == {cls.value for cls in FailureClass}
    assert analysis["by_failure_class"]["UNKNOWN"] == 1
    assert analysis["by_status"]["PASS"] == 1
    assert analysis["failed_tests"][0]["test_id"] == "t1"
