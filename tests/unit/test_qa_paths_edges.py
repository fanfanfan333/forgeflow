"""Edge tests for :mod:`forgeflow.qa.paths` (cwd-independence & overrides).

Complements the foundation suite with the cases that actually matter for a
"works from anywhere" contract: resolution from a *nested* directory, the
nested-``ForgeFlow-main`` child rule, the ``QA_TMP_DIR`` env override, and the
uniqueness/shape guarantees of ``new_run_id``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from forgeflow.qa.paths import QaPaths, find_project_root

_RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")


# ── cwd independence ─────────────────────────────────────────────────────────


def test_resolution_is_independent_of_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    expected = find_project_root()
    monkeypatch.chdir(tmp_path)
    assert find_project_root() == expected
    assert QaPaths.resolve().project_root == expected


def test_resolution_from_a_nested_subdirectory() -> None:
    root = find_project_root()
    nested = root / "forgeflow" / "qa"
    assert nested.is_dir()
    assert QaPaths.resolve(nested).project_root == root


def test_nested_forgeflow_main_child_is_found_from_repo_root() -> None:
    root = find_project_root()
    repo_root = root.parent  # e.g. D:/Agentxm/Multi-Agent contains ForgeFlow-main/
    assert (repo_root / "ForgeFlow-main").is_dir()
    assert QaPaths.resolve(repo_root).project_root == root


# ── QA_PROJECT_ROOT override behaviour ───────────────────────────────────────


def test_qa_project_root_valid_override(monkeypatch: pytest.MonkeyPatch) -> None:
    root = find_project_root()
    monkeypatch.setenv("QA_PROJECT_ROOT", str(root))
    assert find_project_root() == root


def test_qa_project_root_misconfigured_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Has the package dir but no pyproject.toml → not a valid root → refuse to guess.
    (tmp_path / "forgeflow").mkdir()
    (tmp_path / "forgeflow" / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setenv("QA_PROJECT_ROOT", str(tmp_path))
    with pytest.raises(RuntimeError):
        find_project_root()


def test_unresolvable_root_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("QA_PROJECT_ROOT", raising=False)
    with pytest.raises(RuntimeError):
        find_project_root(tmp_path)


def test_resolve_rejects_non_project(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        QaPaths.resolve(tmp_path)


# ── tmp_root override ────────────────────────────────────────────────────────


def test_qa_tmp_dir_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("QA_TMP_DIR", str(tmp_path / "elsewhere"))
    paths = QaPaths(find_project_root())
    assert paths.tmp_root == tmp_path / "elsewhere"
    assert paths.run_dir("r1") == tmp_path / "elsewhere" / "qa" / "r1"


def test_default_tmp_root_is_project_qa_tmp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QA_TMP_DIR", raising=False)
    paths = QaPaths(find_project_root())
    assert paths.tmp_root == find_project_root() / "qa_tmp"
    assert paths.run_dir("r1") == find_project_root() / "qa_tmp" / "qa" / "r1"


# ── run_id shape & uniqueness ────────────────────────────────────────────────


def test_new_run_id_shape_and_uniqueness() -> None:
    moment = datetime(2026, 10, 8, 3, 10, 14, tzinfo=UTC)
    ids = {QaPaths.new_run_id(moment) for _ in range(300)}
    assert len(ids) == 300  # unique even for a frozen instant (uuid suffix)
    assert all(_RUN_ID_RE.match(value) for value in ids)


def test_new_run_id_naive_datetime_is_utc() -> None:
    naive = datetime(2026, 10, 8, 3, 10, 14)  # no tzinfo
    assert QaPaths.new_run_id(naive).startswith("20261008T031014Z-")


def test_new_run_id_is_sortable_by_time() -> None:
    early = QaPaths.new_run_id(datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC))
    late = QaPaths.new_run_id(datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC))
    assert early < late
