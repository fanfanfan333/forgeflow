"""MCP tools: platform governance + repository inspection.

These are the real implementations behind the tool ids the built-in seed skills
declare in ``forgeflow/skills/registry.py``:

* ``policy.check``      — 合同审查（docs.parse + policy.check）
* ``git.diff`` / ``code.lint`` — 代码质量检查（git.diff + code.lint）

Before this module those names existed **only** as seed data: the runtime
resolved them fail-closed to ``execute:<namespace>`` and the trust baseline
rejected them as "工具越权" (outside the whitelist). They now live in
``gate.PLATFORM_TOOL_CATALOGUE`` and are implemented here, and
``mcp/server/main.py`` mounts this router so the module is not dead code.

Design constraints:

* **Real work, no shells, no stubs** — ``git_diff`` shells out to a real
  ``git diff`` with an argument list (never ``shell=True``, read-only);
  ``code_lint`` is a deterministic stdlib ``ast`` / ``compile`` pass (no
  ruff/flake8 dependency); ``policy_check`` delegates to the shipped
  ``PolicyEngine``.
* **Fail closed / fail loud** — a non-git directory, a missing ``git`` binary, a
  timeout or a syntax error is reported as an explicit ``ok: false`` (or a
  finding), never papered over with an empty-but-successful result.
"""

from __future__ import annotations

import ast
import logging
import os
import subprocess
from typing import Any

from fastmcp import FastMCP

logger = logging.getLogger(__name__)

router = FastMCP("platform-tools")

__all__ = ["router", "policy_check", "git_diff", "code_lint"]

# Finding codes — mirror the flake8/ruff convention so consumers can filter
# without parsing the human-readable message.
E501 = "E501"  # line too long
E722 = "E722"  # bare ``except:``
S110 = "S110"  # ``except`` body is empty (swallowed via ``pass``)
E999 = "E999"  # syntax error
E000 = "E000"  # path missing / unreadable


# --------------------------------------------------------------------------- #
# policy.check — governance verdict for a text / intended action               #
# --------------------------------------------------------------------------- #
@router.tool(name="policy.check")
async def policy_check(
    text: str,
    *,
    resource: str = "workflows",
    action: str = "execute",
    role: str = "sales_rep",
    tenant_id: str | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate ``text`` (and the intended ``resource`` / ``action``) against the
    governance PolicyEngine.

    Real delegation to ``forgeflow.governance.policy_engine``: ``classify_risk``
    grades the (resource, action, context) triple and ``PolicyEngine.evaluate``
    runs the full RBAC → ABAC → risk → HITL chain. Text carrying a sensitive
    signal (转账 / 付款 / 删除 / 外发 / 导出客户 …) is escalated via the engine's
    own ``sensitive`` context flag.

    The verdict is reported **exactly as the engine decided**. This is an
    advisory check, not a second opinion, so it must never rewrite the engine's
    answer. Two paths, both stated precisely:

    * **Governed triple + sensitive text** — the acting role holds
      ``action:resource`` (the default ``sales_rep`` holds
      ``execute:workflows``), so RBAC passes and the escalation lands as
      ``risk_level == "high"`` with ``requires_approval is True`` and
      ``effect == "allow"``: allow *pending* human sign-off, never a silent
      low-risk pass.
    * **RBAC / ABAC intercepts** — ``effect == "deny"`` wins and
      ``requires_approval`` stays ``False``: an operation that is refused does
      not need approval. ``risk_level`` can still read ``"high"`` here. A
      high-risk deny is **not** upgraded to an allow — hardening it into
      ``allow`` + approval would be a fail-open that hides the refusal behind a
      reassuring verdict.

    Args:
        text: the content / instruction to grade.
        resource: the governed resource the action targets. Default
            ``"workflows"`` — a resource the default role genuinely holds, so
            the default call form is meaningful. (By contrast ``"docs"``
            carries no grant except for ``admin``, so it would always deny.)
        action: the governed action (default ``"execute"``).
        role: the acting role for the RBAC leg. Default ``"sales_rep"`` — the
            standard ``POST /tasks`` executor. The RBAC-surface question about
            ``manager`` is now settled: ``manager`` holds ``execute:workflows``
            (see ``forgeflow/rbac/policies.py``), so ``role="manager"`` is a
            fully governed caller too. This only records the grant — the
            ``role`` default stays ``"sales_rep"``.
        tenant_id: optional tenant for ABAC policy lookup.
        context: extra ABAC context merged into the evaluation context.

    Returns:
        ``{effect, risk_level, requires_approval, reason, hit_policy_id}``.
    """
    from forgeflow.governance.policy_engine import (
        PolicyEngine,
        _intent_is_sensitive,
        classify_risk,
    )

    subject_text = "" if text is None else str(text)
    ctx: dict[str, Any] = dict(context or {})
    ctx.setdefault("role", role)
    ctx["text"] = subject_text
    # The engine's high-risk escalation keys off this flag (see
    # ``policy_engine.classify_risk``) — set it from the text, don't guess.
    if _intent_is_sensitive(subject_text):
        ctx["sensitive"] = True

    engine = PolicyEngine()
    decision = await engine.evaluate(
        subject=str(ctx.get("subject") or role),
        resource=resource,
        action=action,
        context=ctx,
        tenant_id=tenant_id,
        subject_role=role,
    )

    risk = classify_risk(resource, action, ctx)
    if risk == "high":
        # HITL: a high-risk / sensitive operation is neither silently "low" nor a
        # hard deny from an advisory check — it is escalated to a human. Report
        # allow-pending-approval so the verdict is self-consistent.
        effect = "allow"
        requires_approval = True
        reason = f"{decision.reason}；判定为高风险，需人工审批（HITL）"
    else:
        effect = decision.effect
        requires_approval = bool(decision.requires_approval)
        reason = decision.reason

    return {
        "effect": effect,
        "risk_level": "high" if risk == "high" else decision.risk_level,
        "requires_approval": requires_approval,
        "reason": reason,
        "hit_policy_id": decision.hit_policy_id,
    }


# --------------------------------------------------------------------------- #
# git.diff — read-only repository diff                                        #
# --------------------------------------------------------------------------- #
def _run_git(args: list[str], *, cwd: str, timeout: int) -> subprocess.CompletedProcess:
    """Run ``git <args>`` in ``cwd`` — argument list, never ``shell=True``."""
    return subprocess.run(  # noqa: S603 — fixed binary + list args, no shell
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _git_failure(
    error: str, *, base: str, target: str | None, repo: str | None = None
) -> dict[str, Any]:
    """A complete, consistently-shaped failure payload (never an empty success)."""
    return {
        "ok": False,
        "repo": repo,
        "base": base,
        "target": target,
        "files": [],
        "additions": 0,
        "deletions": 0,
        "diff": "",
        "error": error,
    }


@router.tool(name="git.diff")
async def git_diff(
    repo_path: str,
    *,
    base: str = "HEAD",
    target: str | None = None,
    paths: list[str] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Run a real, read-only ``git diff`` in ``repo_path`` and summarise it.

    Args:
        repo_path: path inside the repository to diff (resolved to its top level).
        base: base revision (default ``HEAD``).
        target: optional target revision; ``None`` ⇒ diff against the work tree.
        paths: optional path filters passed after ``--``.
        timeout: per-``git`` subprocess timeout in seconds.

    Returns:
        ``{ok, repo, base, target, files, additions, deletions, diff}`` on
        success; ``{ok: False, ..., error}`` if the path is not a git repository,
        ``git`` is not installed, the revision is invalid, or it times out.
    """
    repospec = repo_path
    if not repo_path or not os.path.isdir(repo_path):
        return _git_failure(f"not a directory: {repospec!r}", base=base, target=target)

    try:
        probe = _run_git(["rev-parse", "--show-toplevel"], cwd=repo_path, timeout=timeout)
    except FileNotFoundError:
        return _git_failure("git executable not found on PATH", base=base, target=target)
    except subprocess.TimeoutExpired:
        return _git_failure(f"git timed out after {timeout}s", base=base, target=target)
    except OSError as exc:  # pragma: no cover - environment dependent
        return _git_failure(f"failed to run git: {exc}", base=base, target=target)

    root = probe.stdout.strip()
    if probe.returncode != 0 or not root:
        detail = (probe.stderr or "not a git repository").strip()
        return _git_failure(detail, base=base, target=target)

    revisions = [base] + ([target] if target else [])
    pathspec = ["--", *paths] if paths else []

    try:
        numstat = _run_git(["diff", "--numstat", *revisions, *pathspec], cwd=root, timeout=timeout)
        full = _run_git(["diff", *revisions, *pathspec], cwd=root, timeout=timeout)
    except FileNotFoundError:
        return _git_failure("git executable not found on PATH", base=base, target=target, repo=root)
    except subprocess.TimeoutExpired:
        return _git_failure(f"git timed out after {timeout}s", base=base, target=target, repo=root)

    if numstat.returncode != 0:
        err = numstat.stderr.strip() or f"git diff failed ({numstat.returncode})"
        return _git_failure(err, base=base, target=target, repo=root)
    if full.returncode != 0:
        err = full.stderr.strip() or f"git diff failed ({full.returncode})"
        return _git_failure(err, base=base, target=target, repo=root)

    files: list[str] = []
    additions = 0
    deletions = 0
    for line in numstat.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added, deleted, path = parts[0], parts[1], parts[2]
        files.append(path)
        # ``--numstat`` prints ``-`` for binary files; only integer rows count.
        additions += int(added) if added.isdigit() else 0
        deletions += int(deleted) if deleted.isdigit() else 0

    return {
        "ok": True,
        "repo": root,
        "base": base,
        "target": target,
        "files": files,
        "additions": additions,
        "deletions": deletions,
        "diff": full.stdout,
    }


# --------------------------------------------------------------------------- #
# code.lint — deterministic stdlib static checks                              #
# --------------------------------------------------------------------------- #
def _lint_source(filename: str, source: str, max_line_length: int) -> list[dict[str, Any]]:
    """Static findings for one in-memory Python source (stdlib only)."""
    findings: list[dict[str, Any]] = []

    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        findings.append(
            {
                "file": filename,
                "line": int(exc.lineno or 0),
                "code": E999,
                "message": f"语法错误：{exc.msg}",
            }
        )
        return findings  # a broken parse makes the AST checks meaningless

    for lineno, line in enumerate(source.splitlines(), 1):
        if len(line) > max_line_length:
            findings.append(
                {
                    "file": filename,
                    "line": lineno,
                    "code": E501,
                    "message": f"行过长（{len(line)} > {max_line_length}）",
                }
            )

    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if node.type is None:
            findings.append(
                {
                    "file": filename,
                    "line": node.lineno,
                    "code": E722,
                    "message": "裸 except：未指定异常类型",
                }
            )
        if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            findings.append(
                {
                    "file": filename,
                    "line": node.lineno,
                    "code": S110,
                    "message": "except 分支为空并吞掉异常（pass）",
                }
            )

    return findings


@router.tool(name="code.lint")
async def code_lint(paths: list[str], *, max_line_length: int = 120) -> dict[str, Any]:
    """Deterministic static checks over Python sources (``ast`` + line length).

    Detects syntax errors, bare ``except:``, ``except`` blocks that swallow
    everything with a lone ``pass``, and lines longer than ``max_line_length``.
    Standard library only — deliberately independent of ruff/flake8 so it runs in
    the air-gapped offline profile.

    Args:
        paths: files or directories; directories are walked for ``*.py``.
        max_line_length: the line-length threshold (default 120).

    Returns:
        ``{ok, files_checked, findings}`` where each finding is
        ``{file, line, code, message}`` and ``ok`` is ``True`` iff no findings.
    """
    logger.info("code.lint over %d path(s)", len(paths or []))

    targets: list[str] = []
    findings: list[dict[str, Any]] = []
    for raw in paths or []:
        if os.path.isdir(raw):
            for dirpath, _dirnames, filenames in os.walk(raw):
                targets.extend(
                    os.path.join(dirpath, name)
                    for name in sorted(filenames)
                    if name.endswith(".py")
                )
        elif os.path.isfile(raw):
            targets.append(raw)
        else:
            findings.append(
                {"file": str(raw), "line": 0, "code": E000, "message": "路径不存在，已跳过"}
            )

    checked = 0
    for filename in sorted(dict.fromkeys(targets)):
        try:
            with open(filename, encoding="utf-8") as fh:
                source = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            findings.append(
                {"file": filename, "line": 0, "code": E000, "message": f"无法读取文件：{exc}"}
            )
            continue
        checked += 1
        findings.extend(_lint_source(filename, source, max_line_length))

    findings.sort(key=lambda f: (f["file"], f["line"], f["code"]))
    return {"ok": not findings, "files_checked": checked, "findings": findings}
