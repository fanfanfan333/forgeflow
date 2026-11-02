"""Replan error resurrection must not be laundered by an unrelated read.

2026-11-02 anomaly: a final attempt with only a successful
``textfile.inspect`` suppressed resurrection of failed ``code.execute``
tests, and the run ended completed/success. The old rule suppressed
resurrection on ANY successful non-report tool in the new attempt;
the new rule suppresses only on a successful execution of THAT tool,
or on a successful delivery tool (an alternative permitted tool may
still recover the original task).
"""

from forgeflow.runtime import planning as _planning
from forgeflow.runtime.react_executor import _replan_unresolved_errors


def _rec(tool, status, attempt, error="boom"):
    return {"tool": tool, "status": status, "attempt": attempt, "error": error}


def test_read_only_success_does_not_suppress_code_execute_failure():
    records = [
        _rec("code.execute", "error", 0),
        _rec("textfile.inspect", "ok", 1),
    ]
    out = _replan_unresolved_errors(records, 1, [])
    assert out == ["工具 'code.execute' 尚未修复：boom"]


def test_same_tool_success_suppresses_its_own_failure():
    records = [
        _rec("code.execute", "error", 0),
        _rec("code.execute", "ok", 1),
    ]
    assert _replan_unresolved_errors(records, 1, []) == []


def test_delivery_tool_success_suppresses_other_tools_failures():
    records = [
        _rec("textfile.edit", "error", 0),
        _rec("document.edit", "ok", 1),
    ]
    assert _replan_unresolved_errors(records, 1, []) == []


def test_no_success_resurrects_all_latest_failures():
    records = [
        _rec("code.execute", "error", 0),
        _rec("agent.delegate", "refused", 0),
    ]
    out = _replan_unresolved_errors(records, 1, [])
    assert out == [
        "工具 'code.execute' 尚未修复：boom",
        "工具 'agent.delegate' 尚未修复：boom",
    ]


def test_report_render_success_does_not_count():
    records = [
        _rec("code.execute", "error", 0),
        _rec(_planning.REPORT_TOOL, "ok", 1),
    ]
    out = _replan_unresolved_errors(records, 1, [])
    assert out == ["工具 'code.execute' 尚未修复：boom"]


def test_attempt_zero_leaves_errors_untouched():
    records = [_rec("code.execute", "error", 0)]
    assert _replan_unresolved_errors(records, 0, ["kept"]) == ["kept"]


def test_resurrection_does_not_duplicate_existing_message():
    records = [_rec("code.execute", "error", 0)]
    existing = ["工具 'code.execute' 尚未修复：boom"]
    assert _replan_unresolved_errors(records, 1, existing) == existing
