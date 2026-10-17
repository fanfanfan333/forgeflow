"""Unit tests for :mod:`forgeflow.qa.task_runner`."""

from __future__ import annotations

from pathlib import Path

import pytest

from forgeflow.qa.models import ProcessResult, ProcessStatus, TaskSpec
from forgeflow.qa.paths import find_project_root
from forgeflow.qa.process_runner import ProcessRunner
from forgeflow.qa.task_runner import (
    MakeTaskRunnerAdapter,
    TaskRunnerAdapter,
    parse_makefile_targets,
)


def _ok_result() -> ProcessResult:
    return ProcessResult(
        command=["make"],
        cwd="/x",
        env_diff={},
        exit_code=0,
        status=ProcessStatus.OK,
        stdout="",
        stderr="",
        duration_ms=1,
        started_at="2026-01-01T00:00:00+00:00",
        timed_out=False,
        timeout_s=10,
    )


class _FakeRunner:
    """Records the argv of each call and returns a canned result."""

    def __init__(self, result: ProcessResult) -> None:
        self.result = result
        self.calls: list[list[str]] = []

    def run(self, command, *, cwd=None, timeout_s=None, env=None):
        self.calls.append(command)
        return self.result


def _adapter(runner, makefile: Path | None = None) -> MakeTaskRunnerAdapter:
    mf = makefile or (find_project_root() / "Makefile")
    return MakeTaskRunnerAdapter(mf, "PYX", runner, make_exe="make-x")


def test_parse_makefile_excludes_assignments_and_phony():
    text = (
        "# header comment\n"
        "PY ?= python\n"
        "MANAGED := forgeflow/bootstrap\n"
        ".PHONY: unused\n"
        "# foo runs the thing\n"
        "foo:\n"
        "\techo hi\n"
    )
    parsed = parse_makefile_targets(text)
    assert parsed["foo"] == "foo runs the thing"
    assert "PY" not in parsed
    assert "MANAGED" not in parsed
    assert ".PHONY" not in parsed


def test_discover_real_makefile_targets():
    adapter = _adapter(_FakeRunner(_ok_result()))
    targets = {spec.target for spec in adapter.discover_tasks()}
    assert {"check", "lint", "test-unit", "test-integration"} <= targets
    assert adapter.has_target("check") is True
    # The Makefile has no ``integration`` target — the real name is test-integration.
    assert adapter.has_target("integration") is False


def test_discover_only_keeps_whitelisted_targets(tmp_path):
    makefile = tmp_path / "Makefile"
    makefile.write_text("lint:\n\techo x\ncustom-unknown-target:\n\techo y\n", encoding="utf-8")
    adapter = _adapter(_FakeRunner(_ok_result()), makefile=makefile)
    assert {spec.target for spec in adapter.discover_tasks()} == {"lint"}


def test_run_task_builds_make_argv_with_py():
    fake = _FakeRunner(_ok_result())
    adapter = _adapter(fake)
    result = adapter.run_task(TaskSpec(target="check"), run_id="r1")
    assert fake.calls[-1] == ["make-x", "PY=PYX", "check"]
    assert result.ok is True
    assert adapter.get_result("check") is result
    assert adapter.command_for("lint") == ["make-x", "PY=PYX", "lint"]


def test_run_task_rejects_unknown_target():
    adapter = _adapter(_FakeRunner(_ok_result()))
    with pytest.raises(ValueError):
        adapter.run_task(TaskSpec(target="integration"), run_id="r1")


def test_adapter_satisfies_runtime_protocol():
    adapter = _adapter(_FakeRunner(_ok_result()))
    assert isinstance(adapter, TaskRunnerAdapter)
    assert isinstance(ProcessRunner(), ProcessRunner)
