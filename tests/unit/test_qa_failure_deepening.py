"""Phase D failure deepening (analyzer): fingerprints / attribution / candidates.

Two things are pinned here:

* the **additive** deep chain — ``fingerprints`` / ``environment_attribution`` /
  ``root_cause_candidates`` / ``analyze_deep``; and
* the **backwards-compatible** contract — ``analyze()`` still returns exactly the
  five §90 keys, because two existing suites depend on it verbatim.

The counterexamples are deliberate: a fingerprint must be **stable across the
volatile parts of a transcript** (line numbers, absolute paths, timestamps) yet
**differ for a genuinely different root cause**; and an ``UNKNOWN`` failure must
carry a low confidence (never manufactured certainty).
"""

from __future__ import annotations

from datetime import UTC, datetime

from forgeflow.qa.analyzer import FailureClassifier
from forgeflow.qa.models import Evidence, EvidenceStatus, FailureClass

_ANALYZE_KEYS = {"total", "by_status", "by_failure_class", "failed_tests", "environment_failures"}


def _ev(test_id: str, status: EvidenceStatus, failure_class: FailureClass | None) -> Evidence:
    return Evidence(
        evidence_id=f"r:{test_id}",
        run_id="r",
        capability_id="BUILD:check",
        test_id=test_id,
        command=["make", "PY=x", "check"],
        exit_code=0 if status is EvidenceStatus.PASS else 1,
        status=status,
        timestamp=datetime.now(UTC).isoformat(),
        failure_class=failure_class,
    )


# ── backwards compatibility: analyze() is frozen ─────────────────────────────


def test_analyze_return_keys_are_frozen():
    analysis = FailureClassifier().analyze([_ev("t1", EvidenceStatus.FAIL, FailureClass.UNKNOWN)])
    assert set(analysis) == _ANALYZE_KEYS
    assert "phase" not in analysis


# ── fingerprints (§68) ───────────────────────────────────────────────────────


def test_fingerprint_is_stable_across_volatile_parts():
    volatile_a = 'SyntaxError: invalid syntax\n  File "forgeflow/foo.py", line 3'
    volatile_b = (
        'SyntaxError: invalid syntax\n  File "forgeflow/foo.py", line 4096'
        " C:\\Users\\ci\\python.exe 2026-10-08T05:08:17Z"
    )
    classifier = FailureClassifier()
    first = classifier.fingerprints(
        [_ev("t1", EvidenceStatus.FAIL, FailureClass.SOURCE_BUG)],
        transcripts={"t1": volatile_a},
    )
    second = classifier.fingerprints(
        [_ev("t2", EvidenceStatus.FAIL, FailureClass.SOURCE_BUG)],
        transcripts={"t2": volatile_b},
    )
    assert len(first) == 1
    assert first[0].fingerprint == second[0].fingerprint
    assert first[0].signature == second[0].signature


def test_different_root_causes_yield_different_fingerprints():
    classifier = FailureClassifier()
    numpy = classifier.fingerprints(
        [_ev("t1", EvidenceStatus.FAIL, FailureClass.DEPENDENCY_FAILURE)],
        transcripts={"t1": "ModuleNotFoundError: No module named 'numpy'"},
    )
    pandas = classifier.fingerprints(
        [_ev("t2", EvidenceStatus.FAIL, FailureClass.DEPENDENCY_FAILURE)],
        transcripts={"t2": "ModuleNotFoundError: No module named 'pandas'"},
    )
    assert numpy[0].fingerprint != pandas[0].fingerprint


def test_fingerprints_only_include_failures_with_a_class():
    evidence = [
        _ev("pass", EvidenceStatus.PASS, None),
        _ev("fail", EvidenceStatus.FAIL, FailureClass.SOURCE_BUG),
        _ev("skip", EvidenceStatus.SKIP, None),
    ]
    fingerprints = FailureClassifier().fingerprints(
        evidence, transcripts={"fail": "SyntaxError: bad"}
    )
    assert [fp.failure_class for fp in fingerprints] == [FailureClass.SOURCE_BUG]
    assert fingerprints[0].test_ids == ["fail"]


def test_fingerprints_aggregate_test_ids_and_count():
    evidence = [
        _ev("a", EvidenceStatus.FAIL, FailureClass.MIGRATION_FAILURE),
        _ev("b", EvidenceStatus.FAIL, FailureClass.MIGRATION_FAILURE),
    ]
    transcripts = {
        "a": "alembic upgrade head: target database is not up to date",
        "b": "alembic upgrade head: target database is not up to date",
    }
    fingerprints = FailureClassifier().fingerprints(evidence, transcripts=transcripts)
    assert len(fingerprints) == 1
    assert fingerprints[0].count == 2
    assert fingerprints[0].test_ids == ["a", "b"]


def test_fingerprints_without_transcripts_are_deterministic():
    evidence = [_ev("make:check", EvidenceStatus.FAIL, FailureClass.UNKNOWN)]
    first = FailureClassifier().fingerprints(evidence)
    second = FailureClassifier().fingerprints(evidence)
    assert first[0].fingerprint == second[0].fingerprint


# ── analyze_deep is a strict superset ────────────────────────────────────────


def test_analyze_deep_is_a_superset_of_analyze():
    evidence = [_ev("t1", EvidenceStatus.FAIL, FailureClass.ENVIRONMENT_FAILURE)]
    deep = FailureClassifier().analyze_deep(evidence, transcripts={"t1": "connection refused"})
    assert set(deep) >= _ANALYZE_KEYS
    assert deep["phase"] == "D"
    assert set(deep) >= {"fingerprints", "environment_attribution", "root_cause_candidates"}


# ── environment vs source (§41) ──────────────────────────────────────────────


def test_environment_attribution_never_blames_source_for_env():
    evidence = [
        _ev("env", EvidenceStatus.FAIL, FailureClass.ENVIRONMENT_FAILURE),
        _ev("src", EvidenceStatus.FAIL, FailureClass.SOURCE_BUG),
        _ev("unk", EvidenceStatus.BLOCKED, FailureClass.UNKNOWN),
    ]
    attribution = FailureClassifier().environment_attribution(evidence)
    assert attribution["environment"] == 1
    assert attribution["source"] == 1
    assert attribution["unknown"] == 1
    assert attribution["environment_tests"] == ["env"]
    assert "NOT source bugs" in attribution["note"]


def test_environment_attribution_ignores_non_failures():
    evidence = [_ev("pass", EvidenceStatus.PASS, None)]
    attribution = FailureClassifier().environment_attribution(evidence)
    assert attribution["environment"] == 0
    assert attribution["source"] == 0
    assert attribution["unknown"] == 0


# ── root-cause candidates (§90 深链) ─────────────────────────────────────────


def test_root_cause_unknown_is_low_confidence():
    candidates = FailureClassifier().root_cause_candidates(
        [_ev("t", EvidenceStatus.FAIL, FailureClass.UNKNOWN)],
        transcripts={"t": "totally unrecognised boom"},
    )
    assert candidates[0].attribution == "unknown"
    assert candidates[0].confidence <= 0.2


def test_root_cause_environment_attribution():
    candidates = FailureClassifier().root_cause_candidates(
        [_ev("t", EvidenceStatus.FAIL, FailureClass.ENVIRONMENT_FAILURE)],
        transcripts={"t": "connection refused to postgres"},
    )
    assert candidates[0].attribution == "environment"
    assert candidates[0].confidence >= 0.5


def test_root_cause_excerpt_is_bounded():
    long_line = "x" * 1000
    candidates = FailureClassifier().root_cause_candidates(
        [_ev("t", EvidenceStatus.FAIL, FailureClass.SOURCE_BUG)],
        transcripts={"t": f"{long_line}\nmore"},
    )
    assert 0 < len(candidates[0].evidence_excerpt) <= 400


def test_root_cause_without_transcript_has_no_excerpt():
    candidates = FailureClassifier().root_cause_candidates(
        [_ev("t", EvidenceStatus.FAIL, FailureClass.SOURCE_BUG)]
    )
    assert candidates[0].evidence_excerpt == ""
