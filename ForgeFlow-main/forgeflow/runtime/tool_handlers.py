"""Real tool implementations behind the runtime's plan tool ids (INC12 A1).

Why this module exists
----------------------
Before INC12 the runtime's two executors
(``orchestrator._llm_executor`` / ``orchestrator._default_executor``) recorded
every planned step as ``status="ok"`` **without ever invoking a tool** — the
platform could prove *what an agent was not allowed to do*, but not *what it
actually did*. This module supplies the missing layer: one honest, deterministic
handler per plan tool id, returning ``{"ok": bool, ...}`` so
:mod:`forgeflow.runtime.tool_executor` can record a truthful status.

Hard constraints (INC12 scope)
------------------------------
* **Reuse, don't rewrite.** ``research.search`` / ``data.query`` /
  ``policy.check`` / ``git.diff`` / ``code.lint`` delegate to the modules that
  already implement them.
* **Pure standard library for the new handlers.** ``code.run`` /
  ``analysis.score`` / ``docs.parse`` / ``report.render`` use only ``ast`` /
  ``hashlib`` / ``json`` / ``re`` — never an LLM call, never the network.
* **Honest, never pretend.** A handler with no usable input returns
  ``{"ok": False, "not_executed": True, "reason": ...}`` so the executor records
  ``skipped`` rather than a fabricated success. A development stub
  (``data.query``, and ``research.search`` without Tavily) is labelled as such.

Every handler has the uniform signature
``async def handler(args: dict, ctx: ToolCallContext) -> dict``.
"""

from __future__ import annotations

import ast
import hashlib
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "analysis_score",
    "code_run",
    "data_query",
    "docs_parse",
    "git_diff_handler",
    "policy_check_handler",
    "report_render",
    "research_search",
    "code_lint_handler",
    "HANDLERS",
]

# --------------------------------------------------------------------------- #
# Shared helpers                                                               #
# --------------------------------------------------------------------------- #
#: Maximum number of files ``code.run`` will parse in one call (a bounded,
#: deterministic workload; a runaway directory walk is refused, not truncated
#: silently).
_MAX_CODE_FILES = 500


def _text(args: dict[str, Any], *keys: str) -> str:
    """First non-empty string arg among ``keys`` (``""`` when none)."""
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _path_list(args: dict[str, Any]) -> list[str]:
    """Collect path inputs from ``paths`` (list / str) or ``repo_path``."""
    raw = args.get("paths")
    paths: list[str] = []
    if isinstance(raw, (list, tuple)):
        paths.extend(str(p) for p in raw if str(p).strip())
    elif isinstance(raw, str) and raw.strip():
        paths.append(raw)
    repo = args.get("repo_path")
    if isinstance(repo, str) and repo.strip():
        paths.append(repo)
    return paths


def _collect_python_files(paths: list[str]) -> list[str]:
    """Expand a path list into ``*.py`` files (dirs walked, files kept)."""
    targets: list[str] = []
    for raw in paths:
        if os.path.isdir(raw):
            for dirpath, _dirnames, filenames in os.walk(raw):
                targets.extend(
                    os.path.join(dirpath, name)
                    for name in sorted(filenames)
                    if name.endswith(".py")
                )
        elif os.path.isfile(raw) and raw.endswith(".py"):
            targets.append(raw)
    # Deterministic + de-duplicated order.
    return sorted(dict.fromkeys(targets))


# --------------------------------------------------------------------------- #
# research.search — delegate to the shipped web_search (Tavily or dev stub)     #
# --------------------------------------------------------------------------- #
async def research_search(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Web research via the shipped ``search_tools.web_search``.

    Reuses the real ``web_search`` implementation unchanged. When Tavily is not
    configured the underlying function returns a development-stub result; that
    is surfaced honestly here as ``development_stub=True`` (the executor stamps
    it, but the flag is also part of the payload). A missing query is **not**
    executed — it returns ``not_executed`` so the step is recorded ``skipped``.
    """
    from forgeflow.config import get_settings
    from forgeflow.mcp.server.tools.search_tools import web_search

    query = _text(args, "query", "q")
    if not query:
        return {
            "ok": False,
            "not_executed": True,
            "reason": "未提供查询词（query 缺失），未执行",
        }

    tavily = bool(get_settings().is_tavily_enabled())
    try:
        max_results = int(args.get("max_results") or 5)
    except (TypeError, ValueError):
        max_results = 5

    results = await web_search(query, max_results=max_results)

    # ``web_search`` reports a hard failure as a single ``{"error": ...}`` row.
    if isinstance(results, list) and results and "error" in results[0] and "title" not in results[0]:
        return {
            "ok": False,
            "provider": "tavily" if tavily else "development-stub",
            "development_stub": not tavily,
            "query": query,
            "error": str(results[0].get("error")),
            "results": results,
            "summary": f"web_search 失败：{results[0].get('error')}",
        }

    rows = results if isinstance(results, list) else []
    return {
        "ok": True,
        "provider": "tavily" if tavily else "development-stub",
        "development_stub": not tavily,
        "query": query,
        "count": len(rows),
        "results": rows,
        "summary": (
            f"检索到 {len(rows)} 条结果"
            + ("（development-stub，未配置 Tavily）" if not tavily else "")
        ),
    }


# --------------------------------------------------------------------------- #
# data.query — delegate to the shipped query_db (PERMANENTLY a dev stub)        #
# --------------------------------------------------------------------------- #
async def data_query(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Internal data query via the shipped ``data_tools.query_db``.

    ``query_db`` is **permanently a development stub** — it returns hard-coded
    synthetic rows and no real warehouse is wired anywhere on the platform. That
    is reported as ``development_stub=True`` so no consumer can mistake these
    rows for real data.
    """
    from forgeflow.mcp.server.tools.data_tools import query_db

    table = _text(args, "table")
    if not table:
        return {
            "ok": False,
            "not_executed": True,
            "reason": "未提供表名（table 缺失），未执行",
        }

    filters = args.get("filters") if isinstance(args.get("filters"), dict) else None
    try:
        limit = int(args.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10

    rows = await query_db(table, filters=filters, limit=limit)
    if not isinstance(rows, list):
        rows = [rows] if rows is not None else []
    return {
        "ok": True,
        "provider": "development-stub",
        "development_stub": True,
        "table": table,
        "count": len(rows),
        "rows": rows,
        "summary": f"development-stub 返回 {len(rows)} 行（无真实数仓）",
    }


# --------------------------------------------------------------------------- #
# policy.check — delegate to the shipped platform_tools.policy_check            #
# --------------------------------------------------------------------------- #
async def policy_check_handler(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Governance verdict via the shipped ``platform_tools.policy_check``."""
    from forgeflow.mcp.server.tools.platform_tools import policy_check

    text = _text(args, "text", "intent")
    if not text:
        return {
            "ok": False,
            "not_executed": True,
            "reason": "未提供待评估文本（text/intent 缺失），未执行",
        }

    kwargs: dict[str, Any] = {
        "resource": str(args.get("resource") or "workflows"),
        "action": str(args.get("action") or "execute"),
        "role": str(args.get("role") or getattr(ctx, "role", "viewer") or "viewer"),
        "tenant_id": getattr(ctx, "tenant_id", None),
    }
    context = args.get("context")
    if isinstance(context, dict):
        kwargs["context"] = context

    verdict = await policy_check(text, **kwargs)
    return {
        "ok": True,
        "provider": "local-engine",
        "verdict": verdict,
        "summary": f"policy.check effect={verdict.get('effect')} risk={verdict.get('risk_level')}",
    }


# --------------------------------------------------------------------------- #
# git.diff — delegate to the shipped platform_tools.git_diff                    #
# --------------------------------------------------------------------------- #
async def git_diff_handler(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Read-only ``git diff`` via the shipped ``platform_tools.git_diff``."""
    from forgeflow.mcp.server.tools.platform_tools import git_diff

    repo_path = _text(args, "repo_path")
    if not repo_path:
        return {
            "ok": False,
            "not_executed": True,
            "reason": "未提供仓库路径（repo_path 缺失），未执行",
        }

    paths = args.get("paths") if isinstance(args.get("paths"), list) else None
    result = await git_diff(
        repo_path,
        base=str(args.get("base") or "HEAD"),
        target=(str(args["target"]) if args.get("target") else None),
        paths=paths,
    )
    ok = bool(result.get("ok"))
    return {
        "ok": ok,
        "provider": "git-cli",
        "repo": result.get("repo"),
        "files": result.get("files", []),
        "additions": result.get("additions", 0),
        "deletions": result.get("deletions", 0),
        "diff": result.get("diff", ""),
        "error": result.get("error"),
        "summary": (
            f"git diff {len(result.get('files', []))} 文件"
            if ok
            else f"git diff 失败：{result.get('error')}"
        ),
    }


# --------------------------------------------------------------------------- #
# code.lint — delegate to the shipped platform_tools.code_lint                  #
# --------------------------------------------------------------------------- #
async def code_lint_handler(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Deterministic static checks via the shipped ``platform_tools.code_lint``."""
    from forgeflow.mcp.server.tools.platform_tools import code_lint

    paths = _path_list(args)
    if not paths:
        return {
            "ok": False,
            "not_executed": True,
            "reason": "未提供代码输入（paths/repo_path 均缺失），未执行",
        }

    try:
        max_line_length = int(args.get("max_line_length") or 120)
    except (TypeError, ValueError):
        max_line_length = 120

    result = await code_lint(paths, max_line_length=max_line_length)
    findings = result.get("findings", [])
    return {
        "ok": bool(result.get("ok")),
        "provider": "stdlib-ast",
        "files_checked": result.get("files_checked", 0),
        "findings": findings,
        "summary": (
            f"code.lint 检查 {result.get('files_checked', 0)} 文件，{len(findings)} 处问题"
        ),
    }


# --------------------------------------------------------------------------- #
# code.run — DETERMINISTIC code validation (NOT arbitrary execution)            #
# --------------------------------------------------------------------------- #
async def code_run(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """**Validate** the given Python sources by parsing + compiling them.

    ⚠️ Honesty boundary: despite the name ``code.run``, the platform **only
    performs deterministic validation — it never executes arbitrary code**.
    ``ast.parse`` + ``compile()`` build the AST / bytecode in-process but do not
    *run* the program (no ``exec`` / ``eval`` / ``subprocess``). That is the
    whole contract of this handler; it is recorded here and in the INC12 design
    doc so the tool name can never be read as "runs your code".

    Input order: ``args["paths"]`` → ``args["repo_path"]`` → if both are missing
    a ``not_executed`` result is returned (the executor records ``skipped``).

    Returns:
        ``{ok, valid, checked, errors, ...}``. ``valid`` is ``True`` when every
        file parses and compiles; per-file syntax/compile errors are listed in
        ``errors`` with their real file + line.
    """
    import compileall  # noqa: F401  (kept out; compile below is per-source on purpose)

    paths = _path_list(args)
    if not paths:
        return {
            "ok": False,
            "not_executed": True,
            "reason": "未提供代码输入（paths/repo_path 均缺失），未执行",
        }

    files = _collect_python_files(paths)
    if not files:
        return {
            "ok": False,
            "not_executed": True,
            "reason": "未找到可校验的 Python 源文件，未执行",
        }
    if len(files) > _MAX_CODE_FILES:
        return {
            "ok": False,
            "provider": "stdlib-ast",
            "error": f"待校验文件过多（{len(files)} > {_MAX_CODE_FILES}），已拒绝",
            "summary": "code.run 拒绝：输入规模超上限（不静默截断）",
        }

    errors: list[dict[str, Any]] = []
    checked = 0
    for filename in files:
        try:
            with open(filename, encoding="utf-8") as fh:
                source = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            errors.append({"file": filename, "line": 0, "error": f"无法读取文件：{exc}"})
            continue
        checked += 1
        try:
            tree = ast.parse(source, filename=filename)
        except SyntaxError as exc:
            errors.append(
                {"file": filename, "line": int(exc.lineno or 0), "error": f"语法错误：{exc.msg}"}
            )
            continue
        try:
            compile(tree, filename, "exec")
        except (SyntaxError, ValueError) as exc:  # pragma: no cover — defensive
            errors.append(
                {"file": filename, "line": int(getattr(exc, "lineno", 0) or 0),
                 "error": f"编译失败：{exc}"}
            )

    valid = not errors
    return {
        "ok": True,
        "provider": "stdlib-ast",
        "valid": valid,
        "checked": checked,
        "errors": errors,
        "summary": (
            f"校验通过 {checked} 个文件"
            if valid
            else f"校验发现 {len(errors)} 处问题（{checked} 个文件）"
        ),
    }


# --------------------------------------------------------------------------- #
# analysis.score — real, explainable score over THIS run's observations         #
# --------------------------------------------------------------------------- #
def _observation_stats(observations: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute the coverage / success / evidence-density terms (exact formula)."""
    total = len(observations)
    executed = sum(1 for o in observations if o.get("executed") is True)
    succeeded = sum(1 for o in observations if o.get("status") == "ok")
    with_ref = sum(1 for o in observations if o.get("result_ref"))
    coverage = executed / total if total else 0.0
    success_rate = succeeded / executed if executed else 0.0
    evidence_density = with_ref / total if total else 0.0
    # score = 0.40·coverage + 0.40·success_rate + 0.20·evidence_density
    score = round(0.40 * coverage + 0.40 * success_rate + 0.20 * evidence_density, 4)
    return {
        "total": total,
        "executed": executed,
        "succeeded": succeeded,
        "with_result_ref": with_ref,
        "coverage": round(coverage, 4),
        "success_rate": round(success_rate, 4),
        "evidence_density": round(evidence_density, 4),
        "score": score,
    }


async def analysis_score(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Score the observations **this run has already produced**.

    Input: ``args["observations"]`` — the run's prior tool invocations (as
    ``ToolInvocation.to_dict()`` dicts). The score is a transparent, deterministic
    function of three terms (no LLM, no randomness):

      * **coverage** = executed / total — how many planned calls actually ran;
      * **success_rate** = ok / executed — how many of those returned success;
      * **evidence_density** = with_result_ref / total — how many left evidence.

      ``score = 0.40·coverage + 0.40·success_rate + 0.20·evidence_density``.

    With no scoreable input it returns ``not_executed`` (→ ``skipped``).
    """
    observations = args.get("observations")
    if not isinstance(observations, list) or not observations:
        return {
            "ok": False,
            "not_executed": True,
            "reason": "没有可打分的 observation 输入（observations 为空），未执行",
        }

    stats = _observation_stats([o for o in observations if isinstance(o, dict)])
    if stats["total"] == 0:
        return {
            "ok": False,
            "not_executed": True,
            "reason": "observations 不含有效记录，未执行",
        }
    return {
        "ok": True,
        "provider": "stdlib-stats",
        "score": stats["score"],
        "metrics": stats,
        "formula": "0.40*coverage + 0.40*success_rate + 0.20*evidence_density",
        "summary": f"observation 打分 score={stats['score']}（n={stats['total']}）",
    }


# --------------------------------------------------------------------------- #
# docs.parse — structured section parse (real line numbers)                     #
# --------------------------------------------------------------------------- #
def _parse_sections(text: str) -> list[dict[str, Any]]:
    """Split ``text`` into blank-line-delimited sections with real line numbers.

    Each section starts at the first non-empty line (its ``heading``) and spans
    up to the next blank-line boundary. Line numbers are 1-based and refer to the
    original text — this is the seam INC13 (Evidence/Citation) will anchor to.
    """
    lines = text.splitlines()
    sections: list[dict[str, Any]] = []
    start: int | None = None
    for idx, line in enumerate(lines, 1):
        if line.strip():
            if start is None:
                start = idx
        else:
            if start is not None:
                end = idx - 1
                block = lines[start - 1 : end]
                sections.append(_section(block, start, end))
                start = None
    if start is not None:
        end = len(lines)
        block = lines[start - 1 : end]
        sections.append(_section(block, start, end))
    return sections


def _section(block: list[str], line_start: int, line_end: int) -> dict[str, Any]:
    heading = next((ln.strip() for ln in block if ln.strip()), "")
    return {
        "heading": heading[:80],
        "line_start": line_start,
        "line_end": line_end,
        "lines": len(block),
    }


async def docs_parse(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Structurally parse text into a list of sections (stdlib only).

    Input: ``args["text"]`` → ``args["intent"]`` → the context intent. With no
    text input it returns ``not_executed`` (→ ``skipped``). Each returned section
    carries ``heading`` + ``line_start`` + ``line_end`` (real line numbers) so a
    later increment can cite the exact span.
    """
    text = _text(args, "text", "intent") or getattr(ctx, "intent", "") or ""
    if not text.strip():
        return {
            "ok": False,
            "not_executed": True,
            "reason": "没有文本输入（text/intent 均为空），未执行",
        }

    sections = _parse_sections(text)
    return {
        "ok": True,
        "provider": "stdlib-struct",
        "line_count": len(text.splitlines()),
        "section_count": len(sections),
        "sections": sections,
        "summary": f"解析出 {len(sections)} 个 section（{len(text.splitlines())} 行）",
    }


# --------------------------------------------------------------------------- #
# report.render — render THIS run's observations as Markdown                    #
# --------------------------------------------------------------------------- #
def _render_markdown(observations: list[dict[str, Any]], intent: str) -> str:
    """Deterministic Markdown report over the run's observations."""
    lines: list[str] = ["# 运行报告", ""]
    if intent:
        lines.append(f"**意图**：{intent}")
        lines.append("")
    lines.append("| # | 工具 | 状态 | 已执行 | provider | 延迟(ms) | 摘要 |")
    lines.append("|---|------|------|--------|----------|----------|------|")
    for i, obs in enumerate(observations, 1):
        summary = str(obs.get("summary") or "").replace("|", "\\|")
        lines.append(
            f"| {i} | {obs.get('tool', '?')} | {obs.get('status', '?')} "
            f"| {obs.get('executed')} | {obs.get('provider', '?')} "
            f"| {obs.get('latency_ms', 0)} | {summary} |"
        )
    lines.append("")
    executed = sum(1 for o in observations if o.get("executed") is True)
    succeeded = sum(1 for o in observations if o.get("status") == "ok")
    lines.append(f"共 {len(observations)} 步，已执行 {executed}，成功 {succeeded}。")
    return "\n".join(lines)


async def report_render(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Render the run's observations into a Markdown report.

    Input: ``args["observations"]`` (this run's prior invocations). Returns
    ``{ok, content, result_ref, ...}`` where ``result_ref`` is the first 32 hex
    chars of ``sha256(content)``. With nothing to render it returns
    ``not_executed`` (→ ``skipped``).
    """
    observations = args.get("observations")
    if not isinstance(observations, list) or not observations:
        return {
            "ok": False,
            "not_executed": True,
            "reason": "没有可渲染的 observation，未执行",
        }

    intent = _text(args, "intent") or getattr(ctx, "intent", "") or ""
    rows = [o for o in observations if isinstance(o, dict)]
    content = _render_markdown(rows, intent)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {
        "ok": True,
        "provider": "stdlib-render",
        "content": content,
        "result_ref": digest[:32],
        "observation_count": len(rows),
        "summary": f"渲染 {len(rows)} 条 observation 为 Markdown（{len(content)} 字符）",
    }


# --------------------------------------------------------------------------- #
# Registry of handlers (id → callable)                                         #
# --------------------------------------------------------------------------- #
HANDLERS: dict[str, Any] = {
    "research.search": research_search,
    "data.query": data_query,
    "policy.check": policy_check_handler,
    "git.diff": git_diff_handler,
    "code.lint": code_lint_handler,
    "code.run": code_run,
    "analysis.score": analysis_score,
    "docs.parse": docs_parse,
    "report.render": report_render,
}
