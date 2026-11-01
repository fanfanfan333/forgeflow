"""Multi-pattern stuck detection (OpenHands StuckDetector pattern).

The identical-call guard only stops a third identical tool+args call.
These patterns cover what it misses: the same error with varying args,
the same observation with varying args, and A<->B ping-pong.
"""

from forgeflow.agent.loop_guard import detect_stuck_patterns


def _rec(tool, status="ok", error="", ref="", executed=True):
    return {
        "tool": tool,
        "status": status,
        "error": error,
        "summary": error,
        "result_ref": ref,
        "executed": executed,
    }


def test_same_error_with_varying_args_is_stuck():
    records = [
        _rec("research.search", status="error", error="timeout"),
        _rec("textfile.inspect", status="ok", ref="r1"),
        _rec("research.search", status="error", error="timeout"),
        _rec("research.search", status="error", error="timeout"),
    ]
    patterns = detect_stuck_patterns(records)
    assert len(patterns) == 1
    assert patterns[0].key.startswith("same_error:research.search:")
    assert "timeout" in patterns[0].message


def test_two_errors_are_not_stuck():
    records = [
        _rec("research.search", status="error", error="timeout"),
        _rec("research.search", status="error", error="timeout"),
    ]
    assert detect_stuck_patterns(records) == []


def test_same_observation_with_varying_args_is_stuck():
    records = [_rec("textfile.inspect", ref="doc:spec:1") for _ in range(3)]
    patterns = detect_stuck_patterns(records)
    assert len(patterns) == 1
    assert patterns[0].key == "same_observation:textfile.inspect:doc:spec:1"


def test_ping_pong_is_stuck():
    records = [_rec(t) for t in ["a.search", "b.read", "a.search", "b.read", "a.search", "b.read"]]
    patterns = detect_stuck_patterns(records)
    assert len(patterns) == 1
    assert patterns[0].key == "ping_pong:a.search:b.read"


def test_varied_tools_are_not_stuck():
    records = [
        _rec("a.search", ref="r1"),
        _rec("b.read", ref="r2"),
        _rec("c.lint", ref="r3"),
        _rec("a.search", status="error", error="e1"),
    ]
    assert detect_stuck_patterns(records) == []


def test_report_render_is_excluded():
    records = [_rec("report.render", ref="x") for _ in range(3)]
    assert detect_stuck_patterns(records) == []
