"""Verdict-funnel recovery of superseded tool errors (2026-11-02 a0ab777).

36b2fb5's recovery lives only on the react executor's model-final-answer
path, so it never runs for runs that pause at an approval gate, die on a
ceiling, or halt. ``_drop_historical_errors`` enforces the same rule at
the verdict — the choke point every termination path flows through —
with a narrower delegate gate: a failed delegation is load-bearing ONLY
for research (the only requirement delegation can satisfy).
"""

from __future__ import annotations

from types import SimpleNamespace

from forgeflow.runtime.orchestrator import (
    _drop_historical_errors,
    _missing_continuation_error,
)


def _task(intent, invocations):
    return SimpleNamespace(
        intent=intent,
        context={"resources": ["x"], "tool_invocations": invocations},
    )


def _code_execute_ok():
    return {
        "tool": "code.execute",
        "status": "ok",
        "step_id": "s-exec",
        "payload": {
            "ok": True,
            "codeplane": {
                "diff": "--- a/calc.py\n+++ b/calc.py\n",
                "tests": {"measured": True, "verdict": "passed"},
            },
        },
    }


def _delegate_error(detail="子任务撞上限"):
    return {
        "tool": "agent.delegate",
        "status": "error",
        "step_id": "s-del",
        "error": detail,
        "summary": detail,
        "payload": {"ok": False, "role": "code_review", "error": detail},
    }


def _delegate_errors(detail="子任务撞上限"):
    return [
        f"工具 'agent.delegate' 未成功执行：{detail}",
        f"工具 'agent.delegate' 尚未修复：{detail}",
    ]


def test_code_run_with_diff_drops_side_delegation_errors():
    # a0ab777 clean B 739ed7f5 shape: code.execute produced a diff, a
    # side delegation hit its ceiling, yet the run was
    # awaiting_approval/failure on the delegate error alone.
    task = _task("请修改 calc.py 实现加法", [_code_execute_ok(), _delegate_error()])
    assert _drop_historical_errors(task, _delegate_errors(), None) == []


def test_research_blocking_keeps_delegation_errors():
    # A research parent whose research is still unsatisfied genuinely
    # depends on its delegation: the error stays, honestly.
    task = _task(
        "请研究这份资料并总结",
        [
            {"tool": "textfile.inspect", "status": "ok", "step_id": "s-read", "payload": {}},
            _delegate_error(),
        ],
    )
    errors = _delegate_errors()
    assert _drop_historical_errors(task, errors, None) == errors


def test_no_own_evidence_keeps_delegation_errors():
    # A run that produced nothing itself must not shed its only failure.
    task = _task("请修改文件", [_delegate_error()])
    errors = _delegate_errors()
    assert _drop_historical_errors(task, errors, None) == errors


def test_satisfied_research_drops_side_delegation_errors():
    task = _task(
        "请研究这份资料",
        [
            {
                "tool": "research.verify",
                "status": "ok",
                "step_id": "s-ver",
                "payload": {
                    "artifact_ref": "x",
                    "validation": {"citations_ok": True, "claims_supported": True},
                },
            },
            _delegate_error(),
        ],
    )
    assert _drop_historical_errors(task, _delegate_errors(), None) == []


def test_non_delegate_errors_are_never_dropped():
    task = _task("请修改 calc.py 实现加法", [_code_execute_ok(), _delegate_error()])
    errors = _delegate_errors() + ["工具 'textfile.edit' 未成功执行：写入失败"]
    assert _drop_historical_errors(task, errors, None) == [
        "工具 'textfile.edit' 未成功执行：写入失败"
    ]


def test_vacuous_complete_does_not_launder_delegate_errors():
    # 2026-11-02: assess([], []) is now complete=True (vacuous), but
    # a run with no requirements that delivered nothing must still
    # keep its delegate error — a vacuous complete is not evidence.
    # Without the guard this filter would drop the error (complete
    # True + delegate error in the recovered set).
    task = _task("你好", [_delegate_error()])
    errors = _delegate_errors()
    assert _drop_historical_errors(task, errors, None) == errors


def _missing_scope():
    return SimpleNamespace(fingerprint_missing=True, inherited_inputs=None)


def test_missing_fingerprint_with_no_delivery_fails_not_partial():
    # 2026-11-02 4598152 negative 390067dd: inputs withheld, inspects
    # blocked, answer clean — yet completed/partial, because a blocked
    # inspect is not an error and the validator's partial branch fired.
    task = SimpleNamespace(
        intent="请修改文件",
        context={
            "continued_from_artifact_ref": "ref-missing",
            "tool_invocations": [],
        },
    )
    errors = _missing_continuation_error(task, _missing_scope(), [])
    assert len(errors) == 1
    assert "ref-missing" in errors[0]
    assert "失败" in errors[0]


def test_missing_fingerprint_with_unsatisfied_edit_fails():
    # Same rule, non-vacuous shape: a real edit requirement left
    # unverified by withheld inputs also fails, not partial.
    task = SimpleNamespace(
        intent="请修改 calc.py 实现加法",
        context={
            "continued_from_artifact_ref": "ref-missing",
            "tool_invocations": [],
        },
    )
    errors = _missing_continuation_error(task, _missing_scope(), [])
    assert len(errors) == 1
    assert "ref-missing" in errors[0]


def test_missing_fingerprint_with_delivery_is_untouched():
    # A run that delivered despite the missing fingerprint (research
    # requirement satisfied via its own verify) keeps its verdict.
    task = SimpleNamespace(
        intent="请研究这份资料",
        context={
            "continued_from_artifact_ref": "ref-missing",
            "tool_invocations": [
                {
                    "tool": "research.verify",
                    "status": "ok",
                    "step_id": "s-ver",
                    "payload": {
                        "artifact_ref": "x",
                        "validation": {"citations_ok": True, "claims_supported": True},
                    },
                },
            ],
        },
    )
    assert _missing_continuation_error(task, _missing_scope(), []) == []


def test_missing_fingerprint_keeps_existing_errors():
    task = SimpleNamespace(
        intent="请修改文件",
        context={
            "continued_from_artifact_ref": "ref-missing",
            "tool_invocations": [],
        },
    )
    errors = ["工具 'textfile.edit' 未成功执行：写入失败"]
    assert _missing_continuation_error(task, _missing_scope(), errors) == errors


def test_matched_fingerprint_is_untouched():
    task = SimpleNamespace(
        intent="请修改文件",
        context={
            "continued_from_artifact_ref": "ref-x",
            "tool_invocations": [],
        },
    )
    scope = SimpleNamespace(fingerprint_missing=False, inherited_inputs=None)
    assert _missing_continuation_error(task, scope, []) == []


def test_funnel_drop_is_logged_for_observation(caplog):
    # 2026-11-02 4598152 round: ③'s shape recurred (B 1877f713) but no
    # before/after values were collected, so the funnel's action could
    # not be attributed. The log line is the observation.
    import logging

    task = _task("请修改 calc.py 实现加法", [_code_execute_ok(), _delegate_error()])
    with caplog.at_level(logging.INFO, logger="forgeflow.runtime.orchestrator"):
        assert _drop_historical_errors(task, _delegate_errors(), None) == []
    assert "verdict funnel dropped 2 historical errors (2 -> 0)" in caplog.text
