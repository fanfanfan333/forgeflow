"""The three platform tools the seed skills declare are real, not stubs.

``policy.check`` / ``git.diff`` / ``code.lint`` were seed-data names with zero
implementation: the runtime resolved them fail-closed to ``execute:<namespace>``
and ``verify_trust_baseline`` rejected the 合同审查 / 代码质量检查 seeds with
"工具越权". This suite proves the implementations do **real** work (governance
delegation, a real ``git diff``, a real stdlib lint), fail **loudly** on the
error paths (non-git dir, missing git, syntax error, sensitive text), and — via
the guard test — that every seed's declared tools sit inside
``gate.PLATFORM_PLAN_TOOLS`` and pass the pre-publication baseline.
"""

from __future__ import annotations

import inspect
import os
import shutil
import subprocess

import pytest

from forgeflow.mcp.server.tools import platform_tools
from forgeflow.mcp.server.tools.platform_tools import code_lint, git_diff, policy_check
from forgeflow.runtime import gate
from forgeflow.runtime.gate import required_permission
from forgeflow.skills import registry
from forgeflow.skills.trust_baseline import verify_trust_baseline

_GIT = shutil.which("git")
requires_git = pytest.mark.skipif(_GIT is None, reason="git executable not available")


def _git(*args: str, cwd: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )


def _init_repo_with_commit(path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=str(path))
    (path / "a.txt").write_text("hello\n", encoding="utf-8")
    _git("add", "a.txt", cwd=str(path))
    _git(
        "-c", "user.email=t@example.com", "-c", "user.name=Tester",
        "commit", "-q", "-m", "init", cwd=str(path),
    )


# --------------------------------------------------------------------------- #
# policy.check                                                                 #
# --------------------------------------------------------------------------- #
async def test_policy_check_escalates_sensitive_text_to_hitl():
    """转账/删除 text must genuinely become high-risk with a mandatory approval —
    it must never read as low-risk."""
    result = await policy_check("请帮我给供应商转账 100 万，并删除旧合同")

    assert result["risk_level"] == "high"
    assert result["requires_approval"] is True
    assert result["effect"] == "allow"  # allow-pending-approval, not a silent low


async def test_policy_check_allows_a_benign_read_for_an_authorised_role():
    """A benign text on a triple the role does hold passes with low risk and no
    approval — i.e. the engine, not a hardcoded answer, decides."""
    result = await policy_check(
        "汇总本周销售数据", resource="skills", action="read", role="manager"
    )

    assert result["effect"] == "allow"
    assert result["risk_level"] == "low"
    assert result["requires_approval"] is False
    assert result["hit_policy_id"] is None


async def test_policy_check_denies_an_unmapped_action_for_a_role():
    """viewer lacks write:skills ⇒ the RBAC leg really denies (fail-closed)."""
    result = await policy_check("发布新技能", resource="skills", action="write", role="viewer")

    assert result["effect"] == "deny"
    assert result["requires_approval"] is False
    assert "viewer" in result["reason"]


async def test_policy_check_return_shape_is_stable():
    result = await policy_check("普通文本")
    assert set(result) == {
        "effect",
        "risk_level",
        "requires_approval",
        "reason",
        "hit_policy_id",
    }


# --------------------------------------------------------------------------- #
# git.diff                                                                     #
# --------------------------------------------------------------------------- #
@requires_git
async def test_git_diff_reads_a_real_repository(tmp_path):
    repo = tmp_path / "repo"
    _init_repo_with_commit(repo)
    (repo / "a.txt").write_text("hello\nworld\n", encoding="utf-8")  # one added line

    result = await git_diff(str(repo), base="HEAD")

    assert result["ok"] is True
    assert os.path.samefile(result["repo"], str(repo))
    assert result["files"] == ["a.txt"]
    assert result["additions"] >= 1
    assert "world" in result["diff"]


@requires_git
async def test_git_diff_fails_on_a_non_repository(tmp_path, monkeypatch):
    # GIT_CEILING_DIRECTORIES stops git from walking up into an enclosing repo,
    # so this directory is guaranteed to be outside any work tree.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()

    result = await git_diff(str(not_a_repo))

    assert result["ok"] is False
    assert result["files"] == [] and result["diff"] == ""  # never an empty "success"
    assert "error" in result and "not a git repository" in result["error"].lower()


@requires_git
async def test_git_diff_fails_on_a_missing_directory(tmp_path):
    result = await git_diff(str(tmp_path / "does-not-exist"))

    assert result["ok"] is False
    assert "not a directory" in result["error"].lower()


async def test_git_diff_fails_loudly_when_git_is_missing(tmp_path, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(platform_tools.subprocess, "run", _boom)

    result = await git_diff(str(tmp_path))

    assert result["ok"] is False
    assert "git" in result["error"].lower()


# --------------------------------------------------------------------------- #
# code.lint                                                                    #
# --------------------------------------------------------------------------- #
async def test_code_lint_flags_a_syntax_error(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("def broken(:\n    pass\n", encoding="utf-8")

    result = await code_lint([str(bad)])

    assert result["ok"] is False
    assert result["files_checked"] == 1
    assert any(f["code"] == "E999" for f in result["findings"])


async def test_code_lint_flags_bare_and_empty_except(tmp_path):
    src = "def f():\n    try:\n        return 1\n    except:\n        pass\n"
    target = tmp_path / "swallow.py"
    target.write_text(src, encoding="utf-8")

    result = await code_lint([str(target)])

    codes = {f["code"] for f in result["findings"]}
    assert {"E722", "S110"} <= codes  # bare except + empty-body swallow
    assert result["ok"] is False


async def test_code_lint_flags_a_long_line(tmp_path):
    target = tmp_path / "long.py"
    target.write_text("x = '" + "a" * 50 + "'\n", encoding="utf-8")

    result = await code_lint([str(target)], max_line_length=20)

    assert any(f["code"] == "E501" for f in result["findings"])
    assert result["ok"] is False


async def test_code_lint_passes_a_clean_file(tmp_path):
    target = tmp_path / "clean.py"
    target.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    result = await code_lint([str(target)])

    assert result["ok"] is True
    assert result["findings"] == []
    assert result["files_checked"] == 1


async def test_code_lint_walks_a_directory(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("VALUE = 1\n", encoding="utf-8")

    result = await code_lint([str(tmp_path)])

    assert result["files_checked"] == 1
    assert result["ok"] is True


# --------------------------------------------------------------------------- #
# Wiring: catalogue + MCP server + the seed-skills guard                        #
# --------------------------------------------------------------------------- #
def test_new_catalogue_tools_inherit_the_run_permission():
    """The three ids are in the single-source catalogue ⇒ they resolve to the
    coarse run grant, not the fail-closed ``execute:<namespace>`` gap."""
    for tool in ("policy.check", "git.diff", "code.lint"):
        assert tool in gate.PLATFORM_PLAN_TOOLS, tool
        assert required_permission(tool) == ("execute", "workflows"), tool


def test_platform_tools_are_mounted_by_the_server():
    """The new module is wired into the shipping server, not dead code."""
    from forgeflow.mcp.server import main as mcp_main

    assert mcp_main.platform_tools is platform_tools
    source = inspect.getsource(mcp_main)
    assert "platform_tools.router" in source
    assert 'prefix="platform"' in source


def test_seed_skills_declare_only_catalogue_tools():
    """Guard: every tool a featured seed declares is inside the whitelist.

    This is the regression the fix closes — before ``policy.check`` / ``git.diff``
    / ``code.lint`` were added to the catalogue, the 合同审查 and 代码质量检查
    seeds declared tools outside it.
    """
    for seed in registry._FEATURED_SEED:
        declared = set(seed["tools"])
        missing = declared - set(gate.PLATFORM_PLAN_TOOLS)
        assert not missing, (seed["name"], missing)


def test_seed_skills_pass_the_trust_baseline():
    """Guard: the pre-publication baseline accepts all four seeds (the
    "工具越权" failure is gone)."""
    for seed in registry._FEATURED_SEED:
        report = verify_trust_baseline(seed)
        assert report.ok is True, (seed["name"], report.reason)
        assert report.checks["tools_whitelisted"] is True, seed["name"]
