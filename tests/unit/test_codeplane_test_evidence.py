import pytest

from forgeflow.codeplane.tests_verdict import evaluate_test_output


@pytest.mark.parametrize("cases", [[{"outcome": "skipped"}], [{"status": "unknown"}], [None, "bad"]])
def test_no_recognized_test_is_unmeasured(cases):
    result = evaluate_test_output("", 0, cases)
    assert result.measured is False
    assert result.verdict == "unmeasured"


def test_failed_process_cannot_pass_from_structured_cases():
    result = evaluate_test_output("", 1, [{"name": "case", "outcome": "passed"}])
    assert result.measured is True
    assert result.verdict == "failed"


@pytest.mark.parametrize("cases", [None, [{"outcome": "passed"}]])
def test_missing_execution_exit_is_not_success(cases):
    assert evaluate_test_output("1 passed", None, cases).measured is False


def test_real_successful_case_is_preserved():
    result = evaluate_test_output("", 0, [{"name": "case", "outcome": "passed"}])
    assert result.measured and result.passed == 1 and result.verdict == "passed"


def test_node_test_runner_actual_summary():
    result = evaluate_test_output("# tests 2\n# pass 2\n# fail 0\n# cancelled 0\n", 0)
    assert result.passed == 2 and result.verdict == "passed"


def test_vitest_does_not_count_passing_files_as_passing_cases():
    result = evaluate_test_output(" Test Files 1 passed (1)\n      Tests 3 passed (3)\n", 0)
    assert result.passed == 3


def test_node24_spec_reporter_actual_summary():
    result = evaluate_test_output("ℹ tests 1\nℹ pass 1\nℹ fail 0\nℹ cancelled 0\n", 0)
    assert result.measured and result.passed == 1 and result.verdict == "passed"
