"""Edge tests for :mod:`forgeflow.qa.reporter` (verdict matrix + honesty).

* the full §72 verdict matrix, including the "nothing passed ⇒ INCONCLUSIVE,
  never PASS" rule;
* count consistency across ``execution_summary`` / ``failure_analysis``;
* the evidence-pointer invariant: ``execution_summary`` names the file that was
  actually written — including for a ``test_id`` containing a colon
  (``make:lint`` → on-disk ``make_lint.json``, historically reported as the
  dangling ``make:lint.json``). This is now a plain regression test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from forgeflow.qa.config import QaRunConfig
from forgeflow.qa.evidence import EvidenceStore, safe_id
from forgeflow.qa.models import Evidence, EvidenceStatus, FailureClass, Verdict
from forgeflow.qa.reporter import Reporter

# ── helpers ──────────────────────────────────────────────────────────────────


def _ev(
    test_id: str,
    status: EvidenceStatus,
    failure_class: FailureClass | None = None,
    exit_code: int = 0,
) -> Evidence:
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


def _reporter(tmp_path: Path) -> Reporter:
    return Reporter(EvidenceStore(tmp_path / "run"))


# ── verdict matrix (§72) ─────────────────────────────────────────────────────


def test_verdict_all_pass(tmp_path: Path) -> None:
    assert _reporter(tmp_path).verdict([_ev("a", EvidenceStatus.PASS)]) is Verdict.PASS


def test_verdict_fail_suppresses_everything(tmp_path: Path) -> None:
    evidence = [
        _ev("a", EvidenceStatus.PASS),
        _ev("b", EvidenceStatus.BLOCKED),
        _ev("c", EvidenceStatus.FAIL),
    ]
    assert _reporter(tmp_path).verdict(evidence) is Verdict.FAIL


def test_verdict_blocked_without_fail(tmp_path: Path) -> None:
    evidence = [_ev("a", EvidenceStatus.PASS), _ev("b", EvidenceStatus.BLOCKED)]
    assert _reporter(tmp_path).verdict(evidence) is Verdict.BLOCKED


def test_verdict_pass_plus_skip_is_with_warnings(tmp_path: Path) -> None:
    evidence = [_ev("a", EvidenceStatus.PASS), _ev("b", EvidenceStatus.SKIP)]
    assert _reporter(tmp_path).verdict(evidence) is Verdict.PASS_WITH_WARNINGS


def test_verdict_pass_plus_inconclusive_is_with_warnings(tmp_path: Path) -> None:
    evidence = [_ev("a", EvidenceStatus.PASS), _ev("b", EvidenceStatus.INCONCLUSIVE)]
    assert _reporter(tmp_path).verdict(evidence) is Verdict.PASS_WITH_WARNINGS


@pytest.mark.parametrize(
    "evidence",
    [
        [],
        [_ev("a", EvidenceStatus.SKIP)],
        [_ev("a", EvidenceStatus.INCONCLUSIVE)],
        [_ev("a", EvidenceStatus.SKIP), _ev("b", EvidenceStatus.INCONCLUSIVE)],
    ],
)
def test_verdict_without_a_pass_is_never_pass(tmp_path: Path, evidence: list[Evidence]) -> None:
    assert _reporter(tmp_path).verdict(evidence) is Verdict.INCONCLUSIVE


# ── count consistency ────────────────────────────────────────────────────────


def test_execution_summary_counts_are_consistent(tmp_path: Path) -> None:
    evidence = [
        _ev("a", EvidenceStatus.PASS),
        _ev("b", EvidenceStatus.FAIL, FailureClass.TEST_BUG),
        _ev("c", EvidenceStatus.SKIP),
    ]
    summary = _reporter(tmp_path).execution_summary(evidence)
    assert summary["total"] == 3
    assert set(summary["counts"]) == {s.value for s in EvidenceStatus}
    assert sum(summary["counts"].values()) == summary["total"]
    assert len(summary["results"]) == summary["total"]


def test_failure_analysis_only_lists_fail_and_blocked(tmp_path: Path) -> None:
    evidence = [
        _ev("a", EvidenceStatus.PASS),
        _ev("b", EvidenceStatus.FAIL, FailureClass.SOURCE_BUG),
        _ev("c", EvidenceStatus.BLOCKED, FailureClass.ENVIRONMENT_FAILURE),
        _ev("d", EvidenceStatus.INCONCLUSIVE),
    ]
    analysis = _reporter(tmp_path).failure_analysis(evidence)
    listed = {row["test_id"] for row in analysis["failures"]}
    assert listed == {"b", "c"}
    assert set(analysis["by_failure_class"]) == {cls.value for cls in FailureClass}


# ── evidence pointer invariant ───────────────────────────────────────────────


def test_evidence_path_matches_disk_for_safe_id(tmp_path: Path) -> None:
    cfg = QaRunConfig(project_root=tmp_path, artifacts_dir=tmp_path / "run", run_id="r")
    store = EvidenceStore.for_run(cfg)
    ev = _ev("check", EvidenceStatus.PASS)  # already filesystem-safe
    store.save_evidence(ev)
    row = Reporter(store).execution_summary([ev])["results"][0]
    assert row["evidence_path"] == f"evidence/{safe_id('check')}.json"
    assert (store.run_dir / row["evidence_path"]).is_file()


def test_evidence_path_matches_disk_for_colon_test_id(tmp_path: Path) -> None:
    cfg = QaRunConfig(project_root=tmp_path, artifacts_dir=tmp_path / "run", run_id="r")
    store = EvidenceStore.for_run(cfg)
    ev = _ev("make:lint", EvidenceStatus.PASS)  # raw id contains ':'
    store.save_evidence(ev)
    row = Reporter(store).execution_summary([ev])["results"][0]
    assert row["evidence_path"] == f"evidence/{safe_id('make:lint')}.json"
    assert (store.run_dir / row["evidence_path"]).is_file(), (
        f"reported {row['evidence_path']!r} does not exist on disk"
    )
