"""Edge/exception tests for :mod:`forgeflow.qa.process_runner` (§80).

The happy-path file proves exit-0 capture; this file proves the *hard* parts:

* a real timeout turns into ``TIMEOUT`` (never an infinite block);
* a non-executable command becomes ``SPAWN_ERROR`` (not an exception);
* a non-zero exit is ``FAILED`` **with** the real stdout/stderr preserved;
* ``env_diff`` records only the keys that actually changed;
* malformed argv raises ``ValueError`` (the only place the runner may raise).
"""

from __future__ import annotations

import sys

import pytest

from forgeflow.qa.models import ProcessStatus
from forgeflow.qa.process_runner import ProcessRunner


def _sleep_cmd(seconds: float) -> list[str]:
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


# ── argv validation ──────────────────────────────────────────────────────────


def test_empty_argv_raises() -> None:
    with pytest.raises(ValueError):
        ProcessRunner().run([])


def test_non_str_argv_element_raises() -> None:
    with pytest.raises(ValueError):
        ProcessRunner().run([sys.executable, 1])  # type: ignore[list-item]


def test_str_command_is_rejected() -> None:
    with pytest.raises(ValueError):
        ProcessRunner().run("echo hi")  # type: ignore[arg-type]


# ── happy path + capture ─────────────────────────────────────────────────────


def test_success_captures_stdout() -> None:
    result = ProcessRunner().run([sys.executable, "-c", "print('hello-qa')"])
    assert result.status is ProcessStatus.OK
    assert result.exit_code == 0
    assert "hello-qa" in result.stdout
    assert result.timed_out is False


def test_non_zero_exit_is_failed_with_streams() -> None:
    script = "import sys; sys.stdout.write('out-line'); sys.stderr.write('err-line'); sys.exit(3)"
    result = ProcessRunner().run([sys.executable, "-c", script])
    assert result.status is ProcessStatus.FAILED
    assert result.exit_code == 3
    assert "out-line" in result.stdout
    assert "err-line" in result.stderr


# ── timeout ──────────────────────────────────────────────────────────────────


def test_timeout_is_reported_not_raised() -> None:
    result = ProcessRunner().run(_sleep_cmd(5), timeout_s=1)
    assert result.status is ProcessStatus.TIMEOUT
    assert result.timed_out is True
    assert result.exit_code is None
    assert result.timeout_s == 1
    assert result.duration_ms >= 900  # it really waited ~1s


def test_default_timeout_is_used_when_omitted() -> None:
    runner = ProcessRunner(default_timeout_s=42)
    result = runner.run([sys.executable, "-c", "print('x')"])
    assert result.timeout_s == 42


# ── spawn error ──────────────────────────────────────────────────────────────


def test_missing_executable_is_spawn_error() -> None:
    result = ProcessRunner().run(["definitely-not-a-real-binary-xyz-123"])
    assert result.status is ProcessStatus.SPAWN_ERROR
    assert result.exit_code is None
    assert result.stderr  # an OSError description is captured, not swallowed


# ── env_diff ─────────────────────────────────────────────────────────────────


def test_env_diff_records_only_changed_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QA_EDGE_UNCHANGED", "same")
    runner = ProcessRunner(env_overrides={"QA_EDGE_UNCHANGED": "same", "QA_EDGE_CHANGED": "new"})
    result = runner.run([sys.executable, "-c", "print('x')"])
    assert result.env_diff.get("QA_EDGE_CHANGED") == "new"
    assert "QA_EDGE_UNCHANGED" not in result.env_diff


def test_per_call_env_overrides_are_recorded() -> None:
    runner = ProcessRunner()
    result = runner.run(
        [sys.executable, "-c", "print('x')"],
        env={"QA_EDGE_PER_CALL": "v"},
    )
    assert result.env_diff.get("QA_EDGE_PER_CALL") == "v"


def test_cwd_is_resolved_and_recorded(tmp_path) -> None:
    result = ProcessRunner().run([sys.executable, "-c", "print('x')"], cwd=tmp_path)
    assert result.cwd == str(tmp_path.resolve())
