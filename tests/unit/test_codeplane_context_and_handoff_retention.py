"""Context bloat + lost handoff retention (B run 85f9079e at 6d2ecd5).

1. A code.execute ToolMessage must not carry the full timeline /
   workspace events into the model context: the record keeps them,
   the model sees only outcome/diff/tests/handoff/workspace_id, and a
   reused call sees only a short summary, never the diff again.
2. _assemble_codeplane must retain the code.execute payload's handoff
   (source status, document hashes) and compute the target run's own
   diff_sha256 from its merged diff — distinct from the handoff's
   source diff_sha256, which is None for a document-only source.
3. _workspace_diff must exclude generated __pycache__/*.pyc files.
"""

import hashlib
import json

from forgeflow.runtime import orchestrator as orch
from forgeflow.runtime import tool_handlers as handlers
from forgeflow.runtime.react_executor import _code_execute_feedback


def test_code_execute_feedback_is_compact_and_reused_is_shorter():
    payload = {
        "ok": True,
        "summary": "ok",
        "workspace_id": "ws-1",
        "codeplane": {
            "outcome": "succeeded",
            "diff": "--- a/calc.py\n+++ b/calc.py\n+    return a + b\n",
            "tests": {"verdict": "passed", "measured": True, "failed": []},
            "timeline": [{"type": "x"}] * 50,
            "workspace": {"workspace_id": "ws-1", "events": [{"e": 1}] * 50},
        },
        "handoff": {"status": "partial", "documents": [{"title": "规范.docx"}]},
    }
    feedback = json.loads(_code_execute_feedback(payload))
    assert feedback["outcome"] == "succeeded"
    assert "return a + b" in feedback["diff"]
    assert "timeline" not in feedback
    assert "events" not in json.dumps(feedback)
    assert feedback["handoff"]["status"] == "partial"
    reused = _code_execute_feedback({**payload, "reused": True})
    reused_dict = json.loads(reused)
    assert reused_dict["reused"] is True
    assert "diff" not in reused_dict
    assert reused_dict["diff_chars"] == len(payload["codeplane"]["diff"])


def test_assemble_codeplane_retains_handoff_and_own_diff_hash():
    records = [
        {
            "tool": "code.execute",
            "payload": {
                "codeplane": {
                    "outcome": "succeeded",
                    "diff": "--- a/calc.py\n+    return a + b\n",
                    "tests": {"verdict": "passed", "measured": True},
                    "workspace": {"workspace_id": "ws-1"},
                },
                "handoff": {
                    "source_run_id": "a-run",
                    "status": "partial",
                    "documents": [{"title": "规范.docx", "sha256": "abc"}],
                    "diff_sha256": None,
                },
            },
        }
    ]
    merged = orch._assemble_codeplane(records, [])
    assert merged["handoff"]["status"] == "partial"
    assert merged["handoff"]["documents"][0]["sha256"] == "abc"
    # Source diff hash stays None (A had no code diff); target hash is
    # computed from B's own diff and must differ from the source field.
    assert merged["handoff"]["diff_sha256"] is None
    assert merged["diff_sha256"] == hashlib.sha256(merged["diff"].encode("utf-8")).hexdigest()


def test_workspace_diff_excludes_pycache(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "calc.py").write_text("x\n")
    subprocess.run(["git", "add", "calc.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    (repo / "calc.py").write_text("return a + b\n")
    pycache = repo / "__pycache__"
    pycache.mkdir()
    (pycache / "calc.cpython.pyc").write_text("binary")
    diff = handlers._workspace_diff(str(repo))
    assert "return a + b" in diff
    assert "__pycache__" not in diff
    assert ".pyc" not in diff
