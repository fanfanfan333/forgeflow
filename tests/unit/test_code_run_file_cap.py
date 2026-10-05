"""Task C / F-137 — ``code.run`` file-count cap: configurable + honest truncation.

Before: a hard-coded ``_MAX_CODE_FILES = 500`` rejected the *whole* call once a
directory walk found more files, so a real repository (1031 ``.py`` files) had no
success path at all. After: the cap is a ``Settings`` field
(``code_run_max_files``, default ``2000``) and exceeding it bounds the work to
the first N (path-sorted, de-duplicated) files and returns an explicit
``truncated=True`` with the real ``checked`` / ``total`` counts — never a silent
truncation.

The cap is exercised by *shrinking* it (a temp ``Settings`` proxy), so the test
never materialises thousands of files.

Citation discipline: ``file.py::symbol`` anchors, never line numbers.
"""

from __future__ import annotations

import pytest

from forgeflow.runtime import tool_handlers
from forgeflow.runtime.tool_handlers import code_run

pytestmark = pytest.mark.asyncio


class _SettingsProxy:
    """A real ``Settings`` with ``code_run_max_files`` overridden."""

    def __init__(self, real: object, cap: int) -> None:
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_cap", cap)

    def __getattr__(self, item: str) -> object:
        if item == "code_run_max_files":
            return self._cap
        return getattr(self._real, item)


def _write_py_files(directory, count: int) -> None:
    for i in range(count):
        (directory / f"m{i:02d}.py").write_text("x = 1\n", encoding="utf-8")


def _patch_cap(monkeypatch, cap: int) -> None:
    real = tool_handlers.get_settings()
    monkeypatch.setattr(tool_handlers, "get_settings", lambda: _SettingsProxy(real, cap))


async def test_code_run_below_cap_is_not_truncated(tmp_path, monkeypatch):
    """Files ≤ cap ⇒ ok, truncated False, checked == total."""
    _write_py_files(tmp_path, 5)
    _patch_cap(monkeypatch, 10)

    result = await code_run({"paths": [str(tmp_path)]}, ctx=None)

    assert result["ok"] is True
    assert result["truncated"] is False
    assert result["cap"] == 10
    assert result["total"] == 5
    assert result["checked"] == result["total"] == 5
    assert result["valid"] is True


async def test_code_run_above_cap_truncates_honestly(tmp_path, monkeypatch):
    """Files > cap ⇒ ok, truncated True, checked == cap, total == real count."""
    _write_py_files(tmp_path, 5)
    _patch_cap(monkeypatch, 3)

    result = await code_run({"paths": [str(tmp_path)]}, ctx=None)

    assert result["ok"] is True
    assert result["truncated"] is True
    assert result["cap"] == 3
    assert result["total"] == 5
    assert result["checked"] == 3
    assert len(result["analyzed_files"]) == 3
    # The truncation is stated in plain words — never silent.
    assert "上限" in result["summary"] and "5" in result["summary"] and "3" in result["summary"]
    assert "truncated" in result["summary"].lower() or "截断" in result["summary"]


async def test_code_run_keeps_not_executed_branches(tmp_path, monkeypatch):
    """The two ``not_executed`` branches (no input / no .py) are unchanged."""
    no_input = await code_run({}, ctx=None)
    assert no_input["ok"] is False
    assert no_input.get("not_executed") is True

    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "note.txt").write_text("hi\n", encoding="utf-8")
    none_found = await code_run({"paths": [str(empty)]}, ctx=None)
    assert none_found["ok"] is False
    assert none_found.get("not_executed") is True
