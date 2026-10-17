"""Edge tests for :mod:`forgeflow.qa.analyzer` — the honesty-critical classifier.

Two things are pinned here:

* the signature → failure-class table (syntax / dependency / database /
  migration / collection / permission / build / llm / unknown); and
* the **honesty rule**: an ambiguous failure is *never* promoted to
  ``SOURCE_BUG``, and a blocking environment finding always wins.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

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


def _item(*, requires: list[str] | None = None) -> TestPlanItem:
    return TestPlanItem(
        test_id="make:check",
        capability_id="BUILD:check",
        layer=Layer.BUILD,
        test_type=TestType.STATIC,
        description="d",
        requires=requires or [],
    )


def _result(
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
        duration_ms=5,
        started_at=datetime.now(UTC).isoformat(),
        timed_out=timed_out,
        timeout_s=10,
    )


# ── signature table ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected"),
    [
        (
            "",
            'File "forgeflow/foo.py", line 3\nSyntaxError: invalid syntax',
            FailureClass.SOURCE_BUG,
        ),
        ("", 'File "tests/test_x.py", line 1\nSyntaxError: bad token', FailureClass.TEST_BUG),
        ("", "ModuleNotFoundError: No module named 'numpy'", FailureClass.DEPENDENCY_FAILURE),
        ("", "ImportError: cannot import name 'x' from 'y'", FailureClass.DEPENDENCY_FAILURE),
        ("", "connection refused to postgres at localhost:5433", FailureClass.DATABASE_FAILURE),
        ("", "could not connect: asyncpg.InterfaceError", FailureClass.DATABASE_FAILURE),
        ("", "connection refused", FailureClass.ENVIRONMENT_FAILURE),
        ("", "alembic.util.exc.CommandError: migration failed", FailureClass.MIGRATION_FAILURE),
        ("", "target database is not up to date", FailureClass.MIGRATION_FAILURE),
        ("", "errors during collection", FailureClass.TEST_BUG),
        ("no tests ran in 0.01s", "", FailureClass.TEST_BUG),
        ("", "collected 0 items", FailureClass.TEST_BUG),
        ("", "PermissionError: [Errno 13] Permission denied", FailureClass.PERMISSION_FAILURE),
        ("", "HTTP 403 Forbidden from upstream", FailureClass.PERMISSION_FAILURE),
        ("", "failed to build wheel for 'x'", FailureClass.BUILD_FAILURE),
        ("", "llm provider hit a rate limit", FailureClass.LLM_FAILURE),
        ("", "AssertionError: assert 1 == 2", FailureClass.UNKNOWN),
        ("1 failed, 2 passed in 0.5s", "", FailureClass.UNKNOWN),
    ],
    # Explicit ids keep the raw signature words out of pytest's verbose output:
    # the whole-suite ``make check`` stdout is what the classifier scans, so a
    # parametrized id echoing "SyntaxError"/"connection refused" would pollute it.
    ids=[
        "sig_source_syntax",
        "sig_test_syntax",
        "sig_module_not_found",
        "sig_import_error",
        "sig_conn_refused_db",
        "sig_conn_could_not_connect_db",
        "sig_conn_refused_generic",
        "sig_alembic",
        "sig_migration_not_current",
        "sig_collection_error",
        "sig_no_tests_ran",
        "sig_collected_zero",
        "sig_permission_denied",
        "sig_http_403",
        "sig_build_failed",
        "sig_llm_rate_limit",
        "sig_assertion_ambiguous",
        "sig_failed_passed_ambiguous",
    ],
)
def test_signature_table(stdout: str, stderr: str, expected: FailureClass) -> None:
    got = FailureClassifier().classify(_result(stdout=stdout, stderr=stderr), _item())
    assert got is expected


# ── honesty rule: never blame the source without a signature ─────────────────


@pytest.mark.parametrize(
    "text",
    [
        "1 failed, 2 passed in 0.5s",
        "AssertionError: assert 1 == 2",
        "Traceback (most recent call last): ValueError: boom",
        "exit status 1",
        "some totally unrecognised error text",
        "FAILED test_something.py::test_case - assert False",
    ],
    ids=[
        "amb_failed_passed",
        "amb_assertion",
        "amb_traceback_valueerror",
        "amb_exit_status",
        "amb_gibberish",
        "amb_failed_prefix",
    ],
)
def test_ambiguous_failure_is_never_source_bug(text: str) -> None:
    got = FailureClassifier().classify(_result(stdout=text, stderr=text), _item())
    assert got is not FailureClass.SOURCE_BUG


def test_unknown_is_the_default_for_ambiguous() -> None:
    got = FailureClassifier().classify(_result(stdout="boom", stderr="boom"), _item())
    assert got is FailureClass.UNKNOWN


# ── timeout / spawn ──────────────────────────────────────────────────────────


def test_timeout_without_service_requirement_is_tool_failure() -> None:
    got = FailureClassifier().classify(
        _result(status=ProcessStatus.TIMEOUT, timed_out=True), _item()
    )
    assert got is FailureClass.TOOL_FAILURE


def test_timeout_with_service_requirement_is_environment_failure() -> None:
    got = FailureClassifier().classify(
        _result(status=ProcessStatus.TIMEOUT, timed_out=True), _item(requires=["pg"])
    )
    assert got is FailureClass.ENVIRONMENT_FAILURE


def test_spawn_error_is_tool_failure() -> None:
    got = FailureClassifier().classify(
        _result(status=ProcessStatus.SPAWN_ERROR, exit_code=None), _item()
    )
    assert got is FailureClass.TOOL_FAILURE


# ── environment findings win ─────────────────────────────────────────────────


def test_blocking_wrong_python_wins() -> None:
    finding = EnvFinding(check="wrong_python", ok=False, severity="blocking", detail="store stub")
    got = FailureClassifier().classify(_result(), _item(), env_findings=[finding])
    assert got is FailureClass.ENVIRONMENT_FAILURE


def test_ok_finding_is_ignored() -> None:
    finding = EnvFinding(check="wrong_python", ok=True, severity="info", detail="fine")
    got = FailureClassifier().classify(
        _result(stdout="boom", stderr="boom"), _item(), env_findings=[finding]
    )
    assert got is FailureClass.UNKNOWN


def test_service_finding_matches_required_token() -> None:
    finding = EnvFinding(
        check="service_unavailable", ok=False, severity="warning", detail="postgres unreachable"
    )
    got = FailureClassifier().classify(_result(), _item(requires=["pg"]), env_findings=[finding])
    assert got is FailureClass.ENVIRONMENT_FAILURE


def test_service_finding_ignored_when_not_required() -> None:
    finding = EnvFinding(
        check="service_unavailable", ok=False, severity="warning", detail="postgres unreachable"
    )
    got = FailureClassifier().classify(
        _result(stdout="boom", stderr="boom"), _item(), env_findings=[finding]
    )
    assert got is FailureClass.UNKNOWN


# ── aggregation ──────────────────────────────────────────────────────────────


def _evidence(test_id: str, status: EvidenceStatus, failure_class: FailureClass | None) -> Evidence:
    return Evidence(
        evidence_id=f"r:{test_id}",
        run_id="r",
        capability_id="BUILD:check",
        test_id=test_id,
        command=["make", "check"],
        environment={"cwd": "/x"},
        exit_code=0,
        status=status,
        timestamp=datetime.now(UTC).isoformat(),
        failure_class=failure_class,
    )


def test_analyze_aggregates_and_sorts_environment_failures() -> None:
    evidence = [
        _evidence("b", EvidenceStatus.FAIL, FailureClass.ENVIRONMENT_FAILURE),
        _evidence("a", EvidenceStatus.BLOCKED, FailureClass.ENVIRONMENT_FAILURE),
        _evidence("c", EvidenceStatus.PASS, None),
    ]
    analysis = FailureClassifier().analyze(evidence)
    assert analysis["total"] == 3
    assert set(analysis["by_failure_class"]) == {cls.value for cls in FailureClass}
    assert analysis["environment_failures"] == ["a", "b"]
    assert {row["test_id"] for row in analysis["failed_tests"]} == {"a", "b"}
    assert sum(analysis["by_status"].values()) == 3
