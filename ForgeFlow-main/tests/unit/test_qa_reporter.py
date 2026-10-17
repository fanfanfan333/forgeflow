"""Unit tests for :mod:`forgeflow.qa.reporter` (SKIP ≠ PASS)."""

from __future__ import annotations

from datetime import UTC, datetime

from forgeflow.qa.config import QaRunConfig
from forgeflow.qa.evidence import EvidenceStore
from forgeflow.qa.models import (
    CapabilityInventory,
    Evidence,
    EvidenceStatus,
    FailureClass,
    Layer,
    TestPlanItem,
    TestType,
    Verdict,
)
from forgeflow.qa.paths import find_project_root
from forgeflow.qa.reporter import Reporter


def _ev(test_id, status, failure_class=None, exit_code=0) -> Evidence:
    return Evidence(
        evidence_id=f"r:{test_id}",
        run_id="r",
        capability_id="BUILD:check",
        test_id=test_id,
        command=["make", "PY=x", "check"],
        environment={"cwd": "/x"},
        exit_code=exit_code,
        status=status,
        timestamp=datetime.now(UTC).isoformat(),
        duration_ms=1,
        failure_class=failure_class,
    )


def _reporter(tmp_path) -> Reporter:
    return Reporter(EvidenceStore(tmp_path / "run"))


def test_verdict_all_pass(tmp_path):
    assert _reporter(tmp_path).verdict([_ev("a", EvidenceStatus.PASS)]) is Verdict.PASS


def test_verdict_fail_dominates(tmp_path):
    evidence = [_ev("a", EvidenceStatus.PASS), _ev("b", EvidenceStatus.FAIL)]
    assert _reporter(tmp_path).verdict(evidence) is Verdict.FAIL


def test_verdict_blocked_without_fail(tmp_path):
    evidence = [_ev("a", EvidenceStatus.PASS), _ev("b", EvidenceStatus.BLOCKED)]
    assert _reporter(tmp_path).verdict(evidence) is Verdict.BLOCKED


def test_verdict_skip_only_is_inconclusive(tmp_path):
    # SKIP must never be counted as PASS.
    assert _reporter(tmp_path).verdict([_ev("a", EvidenceStatus.SKIP)]) is Verdict.INCONCLUSIVE


def test_verdict_inconclusive_only(tmp_path):
    evidence = [_ev("a", EvidenceStatus.INCONCLUSIVE)]
    assert _reporter(tmp_path).verdict(evidence) is Verdict.INCONCLUSIVE


def test_verdict_pass_with_warnings(tmp_path):
    evidence = [_ev("a", EvidenceStatus.PASS), _ev("b", EvidenceStatus.INCONCLUSIVE)]
    assert _reporter(tmp_path).verdict(evidence) is Verdict.PASS_WITH_WARNINGS


def test_verdict_empty_is_inconclusive(tmp_path):
    assert _reporter(tmp_path).verdict([]) is Verdict.INCONCLUSIVE


def test_execution_summary_counts(tmp_path):
    evidence = [_ev("a", EvidenceStatus.PASS), _ev("b", EvidenceStatus.FAIL)]
    summary = _reporter(tmp_path).execution_summary(evidence)
    assert summary["total"] == 2
    assert summary["counts"]["PASS"] == 1
    assert summary["counts"]["FAIL"] == 1


def test_failure_analysis_has_14_class_keys(tmp_path):
    analysis = _reporter(tmp_path).failure_analysis(
        [_ev("a", EvidenceStatus.FAIL, FailureClass.ENVIRONMENT_FAILURE)]
    )
    assert set(analysis["by_failure_class"]) == {cls.value for cls in FailureClass}
    assert analysis["by_failure_class"]["ENVIRONMENT_FAILURE"] == 1


def test_write_final_report_persists_markdown(tmp_path):
    config = QaRunConfig(
        project_root=find_project_root(), artifacts_dir=tmp_path / "run", run_id="r"
    )
    store = EvidenceStore.for_run(config)
    reporter = Reporter(store)
    evidence = [_ev("make:check", EvidenceStatus.PASS)]
    inventory = CapabilityInventory(
        run_id="r",
        generated_at=datetime.now(UTC).isoformat(),
        root=str(find_project_root()),
        layers={Layer.BUILD: []},
        totals={"_all": 0},
    )
    plan: list[TestPlanItem] = [
        TestPlanItem(
            test_id="make:check",
            capability_id="BUILD:check",
            layer=Layer.BUILD,
            test_type=TestType.STATIC,
            description="d",
        )
    ]
    path = reporter.write_final_report(
        inventory=inventory,
        plan=plan,
        summary=reporter.execution_summary(evidence),
        analysis=reporter.failure_analysis(evidence),
        verdict=Verdict.PASS,
    )
    text = path.read_text(encoding="utf-8")
    assert "verdict: PASS" in text
    assert "SKIP and INCONCLUSIVE" in text
