"""Unit tests for :mod:`forgeflow.qa.process_runner`."""

from __future__ import annotations

import sys

import pytest

from forgeflow.qa.models import ProcessStatus
from forgeflow.qa.process_runner import ProcessRunner


def test_successful_command_captures_stdout_and_exit():
    runner = ProcessRunner(default_timeout_s=30)
    result = runner.run([sys.executable, "-c", "print('hello-qa')"])
    assert result.status is ProcessStatus.OK
    assert result.exit_code == 0
    assert result.timed_out is False
    assert "hello-qa" in result.stdout
    assert result.duration_ms >= 0
    assert result.env_diff == {}
    assert result.cwd


def test_nonzero_exit_is_captured_not_raised():
    runner = ProcessRunner(default_timeout_s=30)
    result = runner.run([sys.executable, "-c", "import sys; sys.exit(3)"])
    assert result.status is ProcessStatus.FAILED
    assert result.exit_code == 3
    assert result.timed_out is False


def test_timeout_is_reported_without_blocking_forever():
    runner = ProcessRunner(default_timeout_s=30)
    result = runner.run(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        timeout_s=1,
    )
    assert result.status is ProcessStatus.TIMEOUT
    assert result.timed_out is True
    assert result.exit_code is None
    assert result.timeout_s == 1


def test_env_diff_records_only_the_difference():
    runner = ProcessRunner(env_overrides={"QA_RUNNER_FLAG": "runner"})
    result = runner.run(
        [sys.executable, "-c", "print('ok')"],
        env={"QA_CALL_FLAG": "call"},
    )
    assert result.env_diff["QA_RUNNER_FLAG"] == "runner"
    assert result.env_diff["QA_CALL_FLAG"] == "call"
    # A key that already matches the parent environment is not recorded.
    assert "PATH" not in result.env_diff


def test_spawn_error_is_reported_as_data(tmp_path):
    runner = ProcessRunner(default_timeout_s=30)
    missing = tmp_path / "definitely-not-an-executable"
    result = runner.run([str(missing)])
    assert result.status is ProcessStatus.SPAWN_ERROR
    assert result.exit_code is None
    assert result.timed_out is False


def test_malformed_command_is_a_programming_error():
    runner = ProcessRunner()
    with pytest.raises(ValueError):
        runner.run([])
    with pytest.raises(ValueError):
        runner.run(["ok", 123])  # type: ignore[list-item]


def test_cwd_is_respected(tmp_path):
    runner = ProcessRunner(default_timeout_s=30)
    result = runner.run(
        [sys.executable, "-c", "import os; print(os.getcwd())"],
        cwd=tmp_path,
    )
    assert result.status is ProcessStatus.OK
    assert str(tmp_path) in result.stdout
