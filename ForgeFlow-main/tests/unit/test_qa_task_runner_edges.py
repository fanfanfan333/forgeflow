"""Edge tests for :mod:`forgeflow.qa.task_runner` (§15/§78).

Pins the "no phantom targets" contract: the parser ignores assignments and
recipe lines, discovery is whitelist-gated, ``has_target`` is the single
existence gate, and ``run_task`` refuses an unknown target with ``ValueError``
*before* spawning anything.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from forgeflow.qa.models import TaskSpec
from forgeflow.qa.process_runner import ProcessRunner
from forgeflow.qa.task_runner import (
    KNOWN_TARGETS,
    MakeTaskRunnerAdapter,
    parse_makefile_targets,
)


def _adapter(makefile: Path) -> MakeTaskRunnerAdapter:
    return MakeTaskRunnerAdapter(makefile, sys.executable, ProcessRunner())


# ── parser ───────────────────────────────────────────────────────────────────


def test_parser_ignores_assignments_and_recipes() -> None:
    text = (
        "PY := python\n"
        "VAR = value\n"
        "\n"
        "# build everything\n"
        "# second comment line\n"
        "check:\n"
        "\t$(PY) -m pytest\n"
        "lint: format-check\n"
        "\t$(PY) -m ruff check\n"
    )
    targets = parse_makefile_targets(text)
    assert "PY" not in targets
    assert "VAR" not in targets
    assert set(targets) >= {"check", "lint"}
    assert targets["check"] == "build everything second comment line"


def test_parser_ignores_dot_prefixed_special_targets() -> None:
    targets = parse_makefile_targets(".PHONY: all\nall:\n\techo hi\n")
    assert ".PHONY" not in targets
    assert "all" in targets


def test_parser_keeps_first_definition_of_a_duplicate() -> None:
    text = "# first\nfoo:\n\techo a\n# second\nfoo:\n\techo b\n"
    assert parse_makefile_targets(text)["foo"] == "first"


def test_parser_breaks_comment_block_on_blank_line() -> None:
    text = "# orphan comment\n\n# real\nbar:\n\techo\n"
    assert parse_makefile_targets(text)["bar"] == "real"


# ── discovery + whitelist ────────────────────────────────────────────────────


def test_discover_sorts_and_whitelists(tmp_path: Path) -> None:
    makefile = tmp_path / "Makefile"
    makefile.write_text("lint:\n\techo\nnot-a-known-target:\n\techo\n", encoding="utf-8")
    specs = _adapter(makefile).discover_tasks()
    names = [spec.target for spec in specs]
    assert names == ["lint"]  # unknown helper rule excluded by the whitelist
    assert "check" in KNOWN_TARGETS


def test_discover_missing_makefile_raises(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        _adapter(tmp_path / "nope" / "Makefile").discover_tasks()


# ── existence gate ───────────────────────────────────────────────────────────


def test_has_target_unknown_is_false(tmp_path: Path) -> None:
    makefile = tmp_path / "Makefile"
    makefile.write_text("lint:\n\techo\n", encoding="utf-8")
    adapter = _adapter(makefile)
    assert adapter.has_target("lint") is True
    assert adapter.has_target("integration") is False  # never invented
    assert adapter.has_target("definitely-not-real") is False


def test_run_task_unknown_target_raises_before_spawning(tmp_path: Path) -> None:
    makefile = tmp_path / "Makefile"
    makefile.write_text("lint:\n\techo\n", encoding="utf-8")
    adapter = _adapter(makefile)
    with pytest.raises(ValueError):
        adapter.run_task(TaskSpec(target="integration"), run_id="r")
    assert adapter.get_result("integration") is None  # nothing ran


def test_command_for_shape(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path / "Makefile")
    assert adapter.command_for("check") == [adapter.make_exe, f"PY={sys.executable}", "check"]
