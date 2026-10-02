"""INC25 P0-B self-proof: the approval→commit closure is honest AND idempotent.

Two defects this pins:

1. ``code.commit`` on an **already-committed** workspace used to report a hard
   error ("nothing to commit, working tree clean") — a false negative that turned
   a real, committed change into ``committed=False`` and made the run fail. The
   fix recognises the flow's own ``forgeflow:`` commit and reports ``True``;
   an **untouched** workspace still reports ``False`` (never a fabricated pass).

2. ``_default_executor`` force-appended a *simulated* failure whenever ``"失败"``
   appeared in the intent — so the canonical code intent ("修复**失败**的测试")
   drove the failure→replan loop and re-invoked ``code.commit``. Code tasks are
   now exempt from the substring heuristic (the explicit context flag still wins).

The counterfactuals below are falsifiable: reverting either fix turns them red.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

from forgeflow.codeplane import workspace as ws_mod
from forgeflow.codeplane.workspace import WorkspaceManager
from forgeflow.runtime.orchestrator import RequestContext, TaskCreate, _simulate_failure
from forgeflow.runtime.tool_handlers import _commit_workspace, code_commit

_GIT_IDENT = {
    "GIT_AUTHOR_NAME": "ForgeFlow",
    "GIT_AUTHOR_EMAIL": "forgeflow@local",
    "GIT_COMMITTER_NAME": "ForgeFlow",
    "GIT_COMMITTER_EMAIL": "forgeflow@local",
}


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    for key, value in _GIT_IDENT.items():
        env.setdefault(key, value)
    return env


def _manager(tmp_path: Path, monkeypatch) -> WorkspaceManager:
    """A manager rooted OUTSIDE the project tree, installed process-wide."""
    mgr = WorkspaceManager(root=str(tmp_path / "cp_root"))
    monkeypatch.setattr(ws_mod, "_MANAGER", mgr, raising=False)
    return mgr


def _seeded_source(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "mathlib.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    for argv in (["git", "init", "-q"], ["git", "add", "-A"],
                 ["git", "commit", "-q", "-m", "baseline"]):
        subprocess.run(argv, cwd=str(src), env=_git_env(), capture_output=True, check=False)
    return src


def _approved(ws_id: str, *, prior: dict | None = None) -> dict:
    return {
        "approval": "approved",
        "workspace_id": ws_id,
        "intent": "修复 mathlib.add 的失败测试",
        "prior": prior or {},
    }


def test_commit_is_idempotent_on_already_committed_workspace(tmp_path, monkeypatch):
    """A second approved commit on an already-committed workspace is TRUE, not error."""
    mgr = _manager(tmp_path, monkeypatch)
    ws = mgr.create("run-idem", {"path": str(_seeded_source(tmp_path))})
    (Path(ws.path) / "mathlib.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8")
    args = _approved(ws.workspace_id, prior={"diff": "diff --git a/mathlib.py b/mathlib.py\n"})

    first = asyncio.run(code_commit(args, None))
    assert first["committed"] is True, first
    assert first["ok"] is True

    # Second invocation: the tree is now clean (`git commit` says nothing to do).
    second = asyncio.run(code_commit(args, None))
    assert second["committed"] is True, second  # idempotent — NOT a false negative
    assert second["ok"] is True
    # The approved diff is still carried forward (the deliverable is not lost).
    assert second["diff"]


def test_commit_on_untouched_workspace_is_honestly_false(tmp_path, monkeypatch):
    """No change was ever made ⇒ committed False (never a fabricated True)."""
    mgr = _manager(tmp_path, monkeypatch)
    ws = mgr.create("run-clean", {"path": str(_seeded_source(tmp_path))})
    res = asyncio.run(code_commit(_approved(ws.workspace_id), None))
    assert res["committed"] is False, res
    assert res["ok"] is False


def test_counterfactual_raw_git_reports_nothing_to_commit(tmp_path):
    """The falsifiable premise: the raw git layer really says "nothing to commit".

    This proves the idempotency wrapper is what converts that into ``True`` — the
    honest raw signal is not being hidden, it is being *interpreted* correctly.
    """
    mgr_root = tmp_path / "cp_root"
    mgr = WorkspaceManager(root=str(mgr_root))
    ws = mgr.create("run-raw", {"path": str(_seeded_source(tmp_path))})
    (Path(ws.path) / "mathlib.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8")

    ok1, _ = _commit_workspace(ws.path, "forgeflow: 修复 mathlib.add 的失败测试")
    assert ok1 is True
    ok2, detail2 = _commit_workspace(ws.path, "forgeflow: 修复 mathlib.add 的失败测试")
    assert ok2 is False
    assert "nothing to commit" in detail2.lower() or "无文件要提交" in detail2


def test_simulate_failure_is_the_explicit_flag_only():
    """INC-41 F-136 — the demo affordance is the explicit flag ONLY.

    The legacy ``"失败" in intent`` substring heuristic was removed: once the
    react path began honouring this helper it force-failed ordinary business
    intents (「分析失败原因」) and injected a fabricated failure reason. This test
    previously pinned that substring — a test defect, corrected here.
    """
    ctx = RequestContext()
    plain = TaskCreate(intent="分析失败原因", context={})
    code = TaskCreate(intent="修复失败的测试", context={"codeplane_approval": "approved"})
    forced = TaskCreate(intent="修复失败的测试",
                        context={"codeplane_approval": "approved", "simulate_failure": True})

    assert _simulate_failure(plain, ctx) is False      # substring no longer fires
    assert _simulate_failure(code, ctx) is False        # no explicit flag ⇒ no failure
    assert _simulate_failure(forced, ctx) is True       # explicit flag is the only trigger
