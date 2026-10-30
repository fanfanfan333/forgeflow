"""Approval-resume record honesty (035fb2f local run, resume 20b6a1e0).

Two gaps in one run: the final codeplane dropped ``applied_to_source``
(it lived only in the code.commit tool record), and a successful
code.commit could never satisfy the edit requirement, so a resume
that committed + applied was always judged partial.
"""

from forgeflow.runtime.acceptance import assess
from forgeflow.runtime.orchestrator import _assemble_codeplane


def _commit_invocation(applied):
    return {
        "tool": "code.commit",
        "status": "ok",
        "payload": {
            "ok": True,
            "committed": True,
            "codeplane": {
                "diff": "diff --git a/calc.py b/calc.py\n",
                "applied_to_source": applied,
                "apply_conflicts": [],
            },
        },
    }


def test_assembled_codeplane_carries_applied_to_source():
    merged = _assemble_codeplane([_commit_invocation(["calc.py"])], [])
    assert merged["applied_to_source"] == ["calc.py"]
    assert merged["apply_conflicts"] == []


def test_code_commit_satisfies_edit_requirement():
    acceptance = assess(
        [{"id": "edit", "kind": "edit", "description": "modify calc.py"}],
        [_commit_invocation(["calc.py"])],
    )
    assert acceptance["complete"] is True
    assert acceptance["requirements"][0]["status"] == "satisfied"


def test_code_commit_with_conflict_does_not_satisfy_edit():
    inv = _commit_invocation([])
    inv["payload"]["codeplane"]["apply_conflicts"] = ["calc.py：源文件已变化"]
    inv["payload"]["ok"] = False
    acceptance = assess(
        [{"id": "edit", "kind": "edit", "description": "modify calc.py"}],
        [inv],
    )
    assert acceptance["complete"] is False


def test_apply_back_normalises_crlf_baseline(tmp_path):
    """Windows default core.autocrlf=true stores LF in git while the
    working-tree file is CRLF; a byte comparison falsely reported a
    moved source. The comparison must normalise line endings."""
    import subprocess

    from forgeflow.runtime.tool_handlers import _apply_workspace_to_source

    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "-C", str(ws), "init", "-q"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(ws), "config", "user.email", "a@b.c"], check=True)
    subprocess.run(["git", "-C", str(ws), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(ws), "config", "core.autocrlf", "true"], check=True)
    (ws / "calc.py").write_bytes(b"raise NotImplementedError('Implement addition')\r\n")
    subprocess.run(["git", "-C", str(ws), "add", "calc.py"], check=True)
    subprocess.run(
        ["git", "-C", str(ws), "commit", "-qm", "forgeflow workspace baseline"], check=True
    )
    (ws / "calc.py").write_bytes(b"def add(a, b):\r\n    return a + b\r\n")
    subprocess.run(["git", "-C", str(ws), "add", "calc.py"], check=True)
    subprocess.run(
        ["git", "-C", str(ws), "commit", "-qm", "forgeflow: implement addition"], check=True
    )
    source = tmp_path / "project-b"
    source.mkdir()
    (source / "calc.py").write_bytes(b"raise NotImplementedError('Implement addition')\r\n")
    diff = "diff --git a/calc.py b/calc.py\n"
    applied, conflicts, error = _apply_workspace_to_source(str(ws), str(source), diff)
    assert error == ""
    assert conflicts == []
    assert applied == ["calc.py"]
