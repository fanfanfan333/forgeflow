"""React loop guards (LangGraph ModelCallLimit / OpenHands stuck-detection).

The model is not a stopping condition, a counter is: when the budget
is nearly gone the next call must be the final answer, and three
identical tool calls in a row is no-progress, not exploration.
"""

from forgeflow.runtime.react_executor import ReactExecutor


def test_third_identical_call_detected():
    hist = [("textfile.inspect", '{"path": "a"}'), ("textfile.inspect", '{"path": "a"}')]
    current = [{"name": "textfile.inspect", "args": {"path": "a"}}]
    assert ReactExecutor._is_third_identical_call(hist, current) is True


def test_different_call_not_detected():
    hist = [("textfile.inspect", '{"path": "a"}'), ("textfile.inspect", '{"path": "a"}')]
    current = [{"name": "textfile.inspect", "args": {"path": "b"}}]
    assert ReactExecutor._is_third_identical_call(hist, current) is False


def test_short_history_not_detected():
    hist = [("textfile.inspect", '{"path": "a"}')]
    current = [{"name": "textfile.inspect", "args": {"path": "a"}}]
    assert ReactExecutor._is_third_identical_call(hist, current) is False
