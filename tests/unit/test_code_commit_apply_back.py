"""Approved commits must reach the declared source tree (B resume b83e23b3).

Until 2026-10-30, approval only committed to the isolated workspace's
own branch and the resume run reported success while the original repo
stayed on ``raise NotImplementedError``. ``_apply_workspace_to_source``
closes that gap, baseline-checked: a source file that moved since the
workspace was created is a conflict, never silently overwritten.
"""

import subprocess

from forgeflow.runtime.tool_handlers import _apply_workspace_to_source

DIFF = "diff --git a/calc.py b/calc.py\n--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-raise NotImplementedError\n+return a + b\n"


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _make_workspace(tmp_path, name):
    ws = tmp_path / name
    ws.mkdir()
    _git(ws, "init", "-q")
    _git(ws, "config", "user.email", "a@b.c")
    _git(ws, "config", "user.name", "t")
    (ws / "calc.py").write_text("raise NotImplementedError('Implement addition')\n")
    _git(ws, "add", "calc.py")
    _git(ws, "commit", "-qm", "forgeflow workspace baseline")
    (ws / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(ws, "add", "calc.py")
    _git(ws, "commit", "-qm", "forgeflow: implement addition")
    return ws


def test_apply_back_updates_source(tmp_path):
    source = tmp_path / "project-b"
    source.mkdir()
    (source / "calc.py").write_text("raise NotImplementedError('Implement addition')\n")
    ws = _make_workspace(tmp_path, "ws")
    applied, conflicts, error = _apply_workspace_to_source(str(ws), str(source), DIFF)
    assert error == ""
    assert conflicts == []
    assert applied == ["calc.py"]
    assert (source / "calc.py").read_text() == "def add(a, b):\n    return a + b\n"


def test_apply_back_is_idempotent(tmp_path):
    source = tmp_path / "project-b"
    source.mkdir()
    (source / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    ws = _make_workspace(tmp_path, "ws")
    applied, conflicts, error = _apply_workspace_to_source(str(ws), str(source), DIFF)
    assert error == ""
    assert applied == ["calc.py"]


def test_apply_back_refuses_moved_source(tmp_path):
    source = tmp_path / "project-b"
    source.mkdir()
    (source / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    ws = _make_workspace(tmp_path, "ws")
    applied, conflicts, error = _apply_workspace_to_source(str(ws), str(source), DIFF)
    assert conflicts
    assert error
    # The moved source is never overwritten.
    assert (source / "calc.py").read_text() == "def add(a, b):\n    return a - b\n"


def test_apply_back_skips_pycache(tmp_path):
    source = tmp_path / "project-b"
    source.mkdir()
    (source / "calc.py").write_text("raise NotImplementedError('Implement addition')\n")
    ws = _make_workspace(tmp_path, "ws")
    pycache = ws / "__pycache__"
    pycache.mkdir()
    (pycache / "calc.cpython.pyc").write_text("binary")
    diff = (
        DIFF
        + "diff --git a/__pycache__/calc.cpython.pyc b/__pycache__/calc.cpython.pyc\nBinary files differ\n"
    )
    applied, conflicts, error = _apply_workspace_to_source(str(ws), str(source), diff)
    assert error == ""
    assert applied == ["calc.py"]
    assert not (source / "__pycache__").exists()
