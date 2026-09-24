"""INC8-A2 — the release gate must not report a clean ``ok`` when there is
nothing comparable.

Old behaviour: a baseline that shares **no** metric with the candidate produced
``findings=[]`` and therefore ``_worst([]) == "ok"`` with the reason
"与上一版本持平或更优" — "never compared" masqueraded as "verified clean". The
case is now surfaced as ``no_baseline`` with ``baseline_present=True`` and an
honest reason, while the genuine clean / warning / regression paths are
untouched.
"""

from __future__ import annotations

from forgeflow.skills.release_gate import evaluate_release


def test_baseline_present_but_no_overlap_is_not_a_clean_ok():
    decision = evaluate_release({"score": 0.9}, {"avg_latency_ms": 100.0})
    assert decision.allowed is True
    assert decision.severity == "no_baseline"
    assert decision.severity != "ok"
    assert decision.baseline_present is True
    assert decision.reason != "与上一版本持平或更优"
    assert "无" in decision.reason  # "基线存在但无可比指标——未做比较"


def test_first_release_is_still_no_baseline_without_baseline_present():
    decision = evaluate_release({"score": 0.9}, None)
    assert decision.severity == "no_baseline"
    assert decision.baseline_present is False


def test_empty_baseline_is_still_no_baseline():
    assert evaluate_release({"score": 0.9}, {}).severity == "no_baseline"


def test_clean_ok_path_is_unchanged():
    decision = evaluate_release({"score": 0.90}, {"score": 0.90})
    assert decision.severity == "ok"
    assert decision.reason == "与上一版本持平或更优"
    assert decision.baseline_present is True
    assert decision.allowed is True


def test_warning_path_is_unchanged():
    decision = evaluate_release({"score": 0.87}, {"score": 0.90})
    assert decision.severity == "warning"
    assert decision.allowed is True


def test_regression_path_is_unchanged():
    decision = evaluate_release({"score": 0.50}, {"score": 0.90})
    assert decision.severity == "regression"
    assert decision.allowed is False
