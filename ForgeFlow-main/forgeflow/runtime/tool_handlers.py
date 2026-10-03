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
  ``blocked`` (the INC15 status for "needed but no valid input") rather than a
  fabricated success. A development stub (``data.query``, and ``research.search``
  without Tavily) is labelled as such.

Every handler has the uniform signature
``async def handler(args: dict, ctx: ToolCallContext) -> dict``.
"""

from __future__ import annotations

import ast
import hashlib
import logging
import os
from typing import Any

from forgeflow.runtime.planning import normalize_status

logger = logging.getLogger(__name__)

__all__ = [
    "analysis_score",
    "analysis_profile",
    "code_run",
    "code_execute",
    "code_commit",
    "data_query",
    "docs_parse",
    "git_diff_handler",
    "policy_check_handler",
    "report_render",
    "research_search",
    "code_lint_handler",
    "document_inspect",
    "document_edit",
    "textfile_inspect",
    "textfile_edit",
    "sheet_inspect",
    "sheet_edit",
    "pdf_inspect",
    "pdf_generate",
    "artifact_save",
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
    executed — it returns ``not_executed`` so the step is recorded ``blocked``.
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
    a ``not_executed`` result is returned (the executor records ``blocked``).

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

    With no scoreable input it returns ``not_executed`` (→ ``blocked``).
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
# analysis.profile — REAL, deterministic profile over a data file's bytes (Q5)  #
# --------------------------------------------------------------------------- #
def _data_paths(args: dict[str, Any]) -> list[str]:
    """The declared data-file paths (``args["paths"]``), normalised to ``[]``."""
    raw = args.get("paths")
    paths: list[str] = []
    if isinstance(raw, (list, tuple)):
        paths.extend(str(p) for p in raw if str(p or "").strip())
    elif isinstance(raw, str) and raw.strip():
        paths.append(raw.strip())
    return paths


def _sum_column(header: list[str], body: list[list[str]], column: str) -> float | None:
    """Sum a column's real numeric cells; ``None`` when the column is absent or
    holds no parseable number (never a fabricated ``0``)."""
    if column not in header:
        return None
    idx = header.index(column)
    values: list[float] = []
    for row in body:
        if idx >= len(row):
            continue
        cell = str(row[idx]).strip()
        if not cell:
            continue
        try:
            values.append(float(cell))
        except ValueError:
            continue
    return sum(values) if values else None


async def analysis_profile(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Profile a delimited data file by reading its **real bytes** (stdlib only).

    This is the INC26 Q5 mechanism — a real, deterministic analysis step. Unlike
    the model, it performs no arithmetic guesswork: it opens the file paths the
    resource layer already dereferenced (``args["paths"]``), parses them with the
    standard-library :mod:`csv` module, and reports two **measured** facts:

      * ``rows`` — the real number of **data** rows (the header is not a row);
      * ``aggregate_value`` — the sum of the target column named by
        ``args["column"]``, over that column's parseable numeric cells.

    Honesty rules (design §1.3):
      * a missing required input (no path, no column, unreadable file) ⇒
        ``not_executed`` (the executor records ``blocked`` — never ``failed``);
      * an **unmeasured** fact is ``None`` — the handler never writes a
        fabricated ``0``; a column that does not exist yields ``aggregate_value
        = None`` with a verbatim note, while ``rows`` (really measured) is kept;
      * it never imports ``openhands`` and never calls ``query_db`` (a permanent
        development stub) — the bytes are the single source of truth.
    """
    import csv as _csv
    import io as _io

    from forgeflow.resources import summaries as _summaries

    paths = _data_paths(args)
    if not paths:
        return {
            "ok": False,
            "not_executed": True,
            "provider": "stdlib-csv",
            "rows": None,
            "aggregate_value": None,
            "reason": "未提供数据文件路径（paths 缺失），未执行",
        }
    column = _text(args, "column")
    if not column:
        return {
            "ok": False,
            "not_executed": True,
            "provider": "stdlib-csv",
            "rows": None,
            "aggregate_value": None,
            "reason": "未提供聚合列（column 缺失），未执行（不猜测列名）",
        }

    target = paths[0]
    try:
        with open(target, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        return {
            "ok": False,
            "not_executed": True,
            "provider": "stdlib-csv",
            "file": target,
            "rows": None,
            "aggregate_value": None,
            "reason": f"数据文件不可读取：{exc}",
        }

    text, _encoding = _summaries._decode(raw)
    delimiter = _summaries._sniff_delimiter(text, target)
    parsed = list(_csv.reader(_io.StringIO(text), delimiter=delimiter))
    if not parsed:
        header: list[str] = []
        body: list[list[str]] = []
    else:
        header = [str(c).strip() for c in parsed[0]]
        body = [row for row in parsed[1:] if any(str(c).strip() for c in row)]

    rows = len(body)
    aggregate_value = _sum_column(header, body, column)
    if column not in header:
        note = f"列不存在：{column}（未做聚合，aggregate_value 为 null）"
    elif aggregate_value is None:
        note = f"列 {column} 无数值可聚合（aggregate_value 为 null）"
    else:
        note = ""
    return {
        "ok": True,
        "provider": "stdlib-csv",
        "file": target,
        "rows": rows,
        "columns": header,
        "aggregate_column": column,
        "aggregate_value": aggregate_value,
        "note": note,
        "summary": (
            f"profile：{rows} 行，{column} 合计 {aggregate_value}"
            if aggregate_value is not None
            else f"profile：{rows} 行，{column} 无可用聚合"
        ),
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
    text input it returns ``not_executed`` (→ ``blocked``). Each returned section
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
#: The text shown in the report body when a per-tool duration was never
#: measured. INC15 (main裁定 §3.1) requires the *report* (a delivered text
#: artifact a person reads) to say the literal Chinese ``未测量`` rather than the
#: ``—`` glyph — a reader parses ``—`` as "this cell is broken", which was the
#: direct source of the complaint. ``—`` is reserved for the *frontend* card,
#: where space is tight (a deliberate density difference over the same
#: ``latency_ms is None`` rule).
_NO_DATA = "未测量"


def _format_latency_ms(value: Any) -> str:
    """Render a latency cell honestly: a *measured* number, or ``未测量``.

    The runtime DOES time every individual tool handler
    (``tool_executor._elapsed_ms``, sampled with ``perf_counter`` around the real
    handler call), so a real run carries a genuine per-tool duration. This
    formatter only prints a value it can trust:

      * a real, positive ``int``/``float`` → ``str(value)`` (sub-millisecond
        precision preserved, e.g. ``0.062``);
      * everything else — missing, ``None`` (never measured), ``0`` (also
        treated as "no measurement", preserving the anti-fabrication guard),
        negatives, non-numerics, ``bool`` — → ``未测量``.

    ``bool`` is rejected explicitly because it is a subclass of ``int`` and a
    stray ``True`` would otherwise render as the number ``1``.
    """
    if isinstance(value, bool):
        return _NO_DATA
    if isinstance(value, (int, float)) and value > 0:
        return str(value)
    return _NO_DATA


def _render_markdown(observations: list[dict[str, Any]], intent: str) -> str:
    """Legacy deterministic Markdown report over a plain observation list.

    Kept byte-for-byte for the ``report_render`` **legacy** call shape (only
    ``observations`` given) so an older caller / test that predates the INC15
    four-layer contract keeps its exact output. A modern call passes ``plan`` and
    ``records`` and goes through :func:`_render_full_report` instead.
    """
    lines: list[str] = ["# 运行报告", ""]
    if intent:
        lines.append(f"**意图**：{intent}")
        lines.append("")
    lines.append("| # | 工具 | 状态 | 已执行 | provider | 延迟(ms) | 摘要 |")
    lines.append("|---|------|------|--------|----------|----------|------|")
    for i, obs in enumerate(observations, 1):
        summary = str(obs.get("summary") or "").replace("|", "\\|")
        latency = _format_latency_ms(obs.get("latency_ms"))
        # INC15 R1 — the status cell goes through ``normalize_status`` so a
        # legacy/exported record whose stored status is ``skipped`` renders as the
        # honest current vocabulary ``blocked``, matching the modern
        # ``_render_full_report`` path instead of leaking the retired literal.
        # The ``?`` marker for a genuinely missing/empty status is preserved.
        status_cell = normalize_status(obs.get("status")) or "?"
        lines.append(
            f"| {i} | {obs.get('tool', '?')} | {status_cell} "
            f"| {obs.get('executed')} | {obs.get('provider', '?')} "
            f"| {latency} | {summary} |"
        )
    lines.append("")
    executed = sum(1 for o in observations if o.get("executed") is True)
    succeeded = sum(1 for o in observations if o.get("status") == "ok")
    lines.append(f"共 {len(observations)} 步，已执行 {executed}，成功 {succeeded}。")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# INC18-B — 关键指标（Key Metrics）
#
# A deliverable is easiest to consume when its numbers stand on their own, so
# the report lifts the scalar values its tools really returned into a small
# table at the top. Three guards keep this from becoming a fabrication engine:
#
#   * **stub data is never a metric.** `development_stub=True` payloads are
#     hard-coded synthetic rows — they must never be presented as a measurement
#     (that is the same rule 来源 already follows).
#   * **only values the tool really returned.** No interpolation, no units
#     invented, no rounding beyond Python's own `str()`; a long string is not a
#     metric (it is prose) and is skipped.
#   * **no section when there is nothing.** An empty result omits the block
#     entirely rather than showing a placeholder zero.
# --------------------------------------------------------------------------- #

#: Payload keys that describe the call itself, not what it measured.
_METRIC_KEY_DENYLIST = frozenset(
    {
        "ok", "status", "executed", "invoked", "provider", "summary", "result_ref",
        "error", "error_type", "not_executed", "reason", "development_stub",
        "formula", "query", "q", "table", "rows", "results", "items", "text",
        "content", "payload", "kind", "tool", "step_id", "arguments_hash",
        "latency_ms", "policy_decision", "approval_id", "data_scope", "tenant_id",
        "agent_id", "run_id", "attempt", "actor_user_id", "actor_role",
        "started_at", "observation_count", "record_count", "count", "limit",
        "filters", "max_results", "intent", "metrics", "object", "path", "paths",
        "namespace", "repo_path", "filters_applied",
        # Truncation envelope keys (`ToolExecutor._bound_payload`). These describe
        # how the payload was cut down, not anything the tool measured — an
        # `original_length` card would be pure noise dressed as a metric.
        "truncated", "original_length", "preview", "sanitized", "redacted", "dropped",
    }
)

_MAX_METRIC_ROWS = 12
#: A metric is a short scalar, not a sentence.
_MAX_METRIC_TEXT_CHARS = 24


def _metric_rows(observations: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """Lift real scalar values out of executed observations → (指标, 数值, 来源)."""
    rows: list[tuple[str, str, str]] = []
    for obs in observations:
        if not isinstance(obs, dict):
            continue
        if obs.get("executed") is not True:
            continue
        # Synthetic data must never masquerade as a measurement.
        if obs.get("development_stub") is True:
            continue
        payload = obs.get("payload")
        if not isinstance(payload, dict):
            continue
        tool = str(obs.get("tool") or "?")
        for key, value in payload.items():
            if key in _METRIC_KEY_DENYLIST or value is None or isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                cell = str(value)
            elif isinstance(value, str):
                text = value.strip()
                if not text or "\n" in text or len(text) > _MAX_METRIC_TEXT_CHARS:
                    continue
                cell = text
            else:
                continue
            rows.append((str(key), cell, tool))
            if len(rows) >= _MAX_METRIC_ROWS:
                return rows
    return rows


def _render_full_report(
    plan: dict[str, Any] | None,
    records: list[dict[str, Any]] | None,
    observations: list[dict[str, Any]] | None,
    intent: str,
    final_answer: str | None = None,
    terminated_by: str | None = None,
) -> str:
    """Render the four distinct layers of the run into one honest Markdown body.

    Sections (each reads its own layer, never a mixed collection):

    * **关键指标** — INC18-B. Scalar values the run's **real, non-stub** tool
      calls actually returned, as a small 指标/数值/来源 table. Derived, never
      estimated; stub payloads are excluded and the whole section is omitted
      when nothing qualifies.
    * **最终答案** — the model's own final answer (INC17), inserted **only** when
      the React loop reached model convergence (``final_answer`` is a non-empty
      string). It is carried **verbatim** — the loop must never let a model answer
      bypass ``report.render`` (L4 is produced *only* here). When the loop was
      cut off by the round ceiling (``terminated_by == "max_iterations"``) an
      honest note is emitted instead, so a half-finished intermediate turn can
      never masquerade as the answer.
    * **执行记录** — the L2 execution records (each row is one record, verbatim:
      status / executed / provider / latency / summary). The latency cell is
      ``未测量`` for a record with ``latency_ms is None`` and ``str(value)`` for a
      real one.
    * **任务计划** — the L1 plan steps.
    * **未适用** — the L1 ``not_applicable`` steps and their reason.
    * **受阻** — the blocked records (needed but missing input).
    * **失败** — the failed records (error / unavailable / refused).

    The footer counts are **recomputed from ``records``** (never summed over a
    mixed collection), and the body explicitly states that the deliverable step
    (``report.render``) itself is not in the execution list — it renders only the
    steps that precede it, so the record count is naturally one short of the plan.

    When neither ``final_answer`` nor a ``max_iterations`` cut-off is supplied the
    output is **byte-for-byte** what it always was (legacy / deterministic / llm
    callers are unaffected — INC17 additive-change discipline).
    """
    plan = plan if isinstance(plan, dict) else {}
    recs = [r for r in (records or []) if isinstance(r, dict)]
    obs = [o for o in (observations or []) if isinstance(o, dict)]
    plan_steps = [s for s in (plan.get("steps") or []) if isinstance(s, dict)]
    na_steps = [s for s in (plan.get("not_applicable") or []) if isinstance(s, dict)]

    lines: list[str] = ["# 运行报告", ""]
    if intent:
        lines += [f"**意图**：{intent}", ""]

    # INC17 — the model's final answer (only when the ReAct loop supplied one), or
    # the honest round-ceiling note. Nothing is emitted when neither applies, so a
    # legacy caller's body is unchanged.
    if isinstance(final_answer, str) and final_answer.strip():
        lines += ["## 最终答案", "", final_answer, ""]
    elif terminated_by == "max_iterations":
        lines += [
            "## 最终答案",
            "",
            "本次运行因达到轮次上限而终止，未产出模型最终答案。",
            "",
        ]
    elif terminated_by == "model":
        # The loop really converged — the model simply stopped calling tools —
        # but its last reply carried no text (a real, observed qwen3 behaviour).
        # Silently omitting the section would leave a "已完成" report that says
        # nothing, so the absence of an answer is stated as plainly as its
        # presence would be.
        lines += [
            "## 最终答案",
            "",
            "模型已结束推理（不再发起工具调用），但未返回任何文本，本次运行没有最终答案。",
            "",
        ]

    # ⓪ 关键指标 — INC18-B. Derived **only** from values a real (non-stub)
    # tool call returned; omitted entirely when there is nothing to show.
    metric_rows = _metric_rows(obs)
    if metric_rows:
        lines += [
            "## 关键指标（Key Metrics）",
            "",
            "下列数值直接取自本次真实执行过的工具返回，未经加工或估算；"
            "开发替身（development stub）产生的合成数据不计入。",
            "",
            "| 指标 | 数值 | 来源 |",
            "|---|---|---|",
        ]
        for key, value, tool in metric_rows:
            lines.append(f"| {key} | {value} | {tool} |")
        lines.append("")

    # ① 执行记录 — the L2 execution records.
    lines += ["## 一、执行记录（Execution Records）", ""]
    if recs:
        lines.append("| # | 工具 | 状态 | 已执行 | provider | 延迟(ms) | 摘要 |")
        lines.append("|---|------|------|--------|----------|----------|------|")
        for i, rec in enumerate(recs, 1):
            summary = str(rec.get("summary") or "").replace("|", "\\|")
            latency = _format_latency_ms(rec.get("latency_ms"))
            lines.append(
                f"| {i} | {rec.get('tool', '?')} | {normalize_status(rec.get('status'))} "
                f"| {rec.get('executed')} | {rec.get('provider', '?')} "
                f"| {latency} | {summary} |"
            )
    else:
        lines.append("（本产物步之前没有真正执行的步骤）")
    lines.append("")

    # ② 任务计划 — the L1 plan.
    lines += ["## 二、任务计划（Task Plan）", ""]
    if plan_steps:
        for step in plan_steps:
            note = f"：{step.get('note')}" if step.get("note") else ""
            lines.append(
                f"- [{step.get('index')}] {step.get('tool')}"
                f"（适用性：{step.get('applicability')}）{note}"
            )
    else:
        lines.append("（无计划步骤）")
    lines.append("")

    # ③ 未适用 — the L1 not_applicable steps.
    lines += ["## 三、未适用（NOT_APPLICABLE）", ""]
    if na_steps:
        for step in na_steps:
            lines.append(f"- {step.get('tool')}：{step.get('reason')}")
    else:
        lines.append("（无）")
    lines.append("")

    blocked = [r for r in recs if normalize_status(r.get("status")) == "blocked"]
    failed = [
        r for r in recs if normalize_status(r.get("status")) in ("error", "unavailable", "refused")
    ]

    # ④ 受阻 — blocked records (needed but missing input).
    lines += ["## 四、受阻（BLOCKED）", ""]
    if blocked:
        for rec in blocked:
            lines.append(f"- {rec.get('tool')}：{rec.get('summary') or rec.get('error') or ''}")
    else:
        lines.append("（无）")
    lines.append("")

    # ⑤ 失败 — failed records.
    lines += ["## 五、失败（FAILED）", ""]
    if failed:
        for rec in failed:
            lines.append(
                f"- {rec.get('tool')}（{normalize_status(rec.get('status'))}）："
                f"{rec.get('error') or rec.get('summary') or ''}"
            )
    else:
        lines.append("（无）")
    lines.append("")

    # Footer counts — recomputed independently from the L2 records.
    executed = sum(1 for r in recs if r.get("executed") is True)
    succeeded = sum(1 for r in recs if r.get("status") == "ok")
    lines.append(
        f"共 {len(plan_steps)} 步计划：已执行 {executed}，成功 {succeeded}，"
        f"受阻 {len(blocked)}，失败 {len(failed)}，未适用 {len(na_steps)}。"
    )
    lines.append("")
    lines.append(
        "说明：本报告由产物步 report.render 生成，它自身不在上方的执行记录中"
        "（它渲染的是在它之前执行过的步骤），因此执行记录条数比计划少 1。"
    )
    return "\n".join(lines)


async def report_render(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Render the run into a Markdown report (the L4 deliverable).

    Two call shapes:

    * **modern (INC15)** — ``args["plan"]`` and ``args["records"]`` present: the
      report renders the four distinct layers (执行记录 / 任务计划 / 未适用 / 受阻 /
      失败) via :func:`_render_full_report`, with the footer counts recomputed
      from the records. This is **always renderable** (even a run where nothing
      executed yields an honest report of the plan), so the deliverable step
      never blocks.
    * **legacy** — only ``args["observations"]`` given: the exact pre-INC15
      ``_render_markdown`` output is preserved byte-for-byte.

    INC17 — two **optional** extra keys on the modern shape, both additive:

    * ``args["final_answer"]`` — the model's own final answer (a non-empty string)
      from the ReAct loop; rendered under ``## 最终答案`` **verbatim**. This is the
      only way a model answer reaches ``result-body`` (L4 is produced solely here).
    * ``args["terminated_by"]`` — ``"model"`` / ``"max_iterations"`` / ``"halted"``.
      ``"max_iterations"`` (with no ``final_answer``) renders the honest
      "因达到轮次上限终止" note instead of a fabricated answer.

    When neither key is supplied the modern output is **byte-for-byte unchanged**.

    Returns ``{ok, content, result_ref, observation_count, ...}`` where
    ``result_ref`` is the first 32 hex chars of ``sha256(content)``.
    """
    observations = args.get("observations")
    plan = args.get("plan")
    raw_records = args.get("records")
    intent = _text(args, "intent") or getattr(ctx, "intent", "") or ""
    # INC17 — additive, optional. A missing key leaves these ``None`` so the
    # rendered body is identical to the pre-INC17 output.
    raw_final_answer = args.get("final_answer")
    final_answer = raw_final_answer if isinstance(raw_final_answer, str) else None
    raw_terminated_by = args.get("terminated_by")
    terminated_by = raw_terminated_by if isinstance(raw_terminated_by, str) else None

    modern = isinstance(plan, dict) or isinstance(raw_records, list)
    if modern:
        records = [r for r in (raw_records or []) if isinstance(r, dict)]
        if isinstance(observations, list):
            obs = [o for o in observations if isinstance(o, dict)]
        else:
            obs = [r for r in records if r.get("executed") is True]
        content = _render_full_report(
            plan, records, obs, intent, final_answer=final_answer, terminated_by=terminated_by
        )
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return {
            "ok": True,
            "provider": "stdlib-render",
            "content": content,
            "result_ref": digest[:32],
            "observation_count": len(obs),
            "record_count": len(records),
            "summary": (
                f"渲染 {len(records)} 条执行记录 / {len(obs)} 条 observation "
                f"为 Markdown（{len(content)} 字符）"
            ),
        }

    # Legacy path — observations only (pre-INC15 callers / tests).
    if not isinstance(observations, list) or not observations:
        return {
            "ok": False,
            "not_executed": True,
            "reason": "没有可渲染的 observation，未执行",
        }

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
# code.execute / code.commit — the code-execution plane (INC25 W2)              #
# --------------------------------------------------------------------------- #
#: The workspace git commit author identity (kept local so a container with no
#: configured git user still commits the baseline / the change).
_GIT_IDENT: dict[str, str] = {
    "GIT_AUTHOR_NAME": "ForgeFlow",
    "GIT_AUTHOR_EMAIL": "forgeflow@local",
    "GIT_COMMITTER_NAME": "ForgeFlow",
    "GIT_COMMITTER_EMAIL": "forgeflow@local",
}


def _resource_ids(args: dict[str, Any]) -> list[str]:
    """The declared resource ids (``args["resource_ids"]``), or ``[]``."""
    raw = args.get("resource_ids")
    if isinstance(raw, (list, tuple)):
        return [str(r) for r in raw if str(r or "").strip()]
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    return []


def _codeplane_source(args: dict[str, Any]) -> Any:
    """The workspace source for ``code.execute`` (a repo path, else first path).

    Returns ``None`` when the task declared no code input — an empty workspace is
    still valid (the engine can run on it); nothing is guessed.
    """
    repo_path = _text(args, "repo_path")
    if repo_path:
        return {"path": repo_path}
    paths = _path_list(args)
    if paths:
        return {"path": paths[0]}
    return None


#: INC28 W4 — the visible summary per derived code-task ``outcome``.
_CODE_OUTCOME_SUMMARY: dict[str, str] = {
    "succeeded": "代码任务已执行：产出变更并通过测试",
    "failed": "代码任务已执行：测试未通过",
    "no_change": "代码任务已执行：本次无代码变更",
    "degraded": "代码任务执行降级",
}


def _derive_code_outcome(
    *, status: str, degraded: str | None, diff: str, tests: dict[str, Any]
) -> str:
    """Return the code task's explicit ``outcome`` (INC28 W4).

    Mechanically derived from evidence ForgeFlow already produced — **never a
    second judgement**:

      1. the engine degraded (or never ran cleanly) ⇒ ``degraded``;
      2. the reviewer's test verdict is ``failed`` ⇒ ``failed``;
      3. no change was produced at all ⇒ ``no_change``;
      4. a change was produced and the tests are not red ⇒ ``succeeded``.

    The ``tests`` verdict is the one ``forgeflow/codeplane/tests_verdict.py``
    (:func:`evaluate_test_output`) owns; this function only *reads* it. The point
    of the field is that ``status == "ok"`` (the engine ran) must never be shown
    as "成功": a run that changed nothing, or whose tests are red, is not a
    success.
    """
    if status != "ok" or degraded:
        return "degraded"
    verdict = str((tests or {}).get("verdict") or "").strip().lower()
    if verdict == "failed":
        return "failed"
    if not str(diff or "").strip():
        return "no_change"
    return "succeeded"


async def code_execute(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Run a code task in an isolated workspace through the subprocess engine.

    This is the **real** code execution step (INC25 W2). Unlike ``code.run`` — which
    only validates sources with ``ast`` and **never** executes anything — this
    handler creates a task-scoped workspace **outside** the project tree, drives the
    OpenHands runner over a process boundary, and returns the produced diff + the
    reviewed test evidence. It never writes the target repository.

    Engine-optional (design §7): when the engine is unavailable the handler returns
    ``{"ok": False, "unavailable": True, "reason": <verbatim>}`` so the executor
    records an honest ``unavailable`` step (never a fake success), and the verbatim
    reason travels into ``codeplane.degraded == "engine_unavailable"``.

    On an **approval resume** (``args["approval"]`` set + ``args["workspace_id"]``)
    the existing workspace is reused and the engine is **not** re-run — the diff /
    test evidence from the first run is carried forward verbatim.

    INC28 W4 — the payload / ``codeplane`` sub-dict carry an explicit ``outcome``
    (``succeeded | failed | no_change | degraded``), derived mechanically from the
    real workspace diff, the reviewer's test verdict
    (``forgeflow/codeplane/tests_verdict.py``) and the engine's ``degraded`` value
    (see :func:`_derive_code_outcome`). ``ok`` is ``True`` **only** when the
    outcome is ``succeeded``: a run that produced no change, or whose tests are
    red, is a business failure — "the engine ran" (``result.status == "ok"``) was
    previously indistinguishable from success, which is exactly the fake-green
    this closes. The approval-resume branch is a projection of prior evidence
    (its ``ok`` is the resume's own success) and is left unchanged.
    """
    from forgeflow.codeplane.engine import CodeJob, get_code_engine
    from forgeflow.codeplane.workspace import get_workspace_manager

    run_id = str(getattr(ctx, "run_id", "") or args.get("run_id") or "")
    intent = _text(args, "intent") or str(getattr(ctx, "intent", "") or "")
    tenant_id = getattr(ctx, "tenant_id", None)
    approval = str(args.get("approval") or "").strip()
    resume_workspace_id = str(args.get("workspace_id") or "").strip()
    prior = args.get("prior") if isinstance(args.get("prior"), dict) else {}

    engine = get_code_engine()

    # --- Resume: reuse the workspace, never re-run the engine. --------------- #
    if approval and resume_workspace_id:
        wm = get_workspace_manager()
        ws = wm.get(resume_workspace_id)
        cp: dict[str, Any] = {
            "engine": {
                "available": True,
                "interpreter": str(getattr(engine, "interpreter", "") or ""),
            },
            "degraded": prior.get("degraded"),
            "workspace": ws.to_dict() if ws is not None else dict(prior.get("workspace") or {}),
            "timeline": [e for e in (prior.get("timeline") or []) if isinstance(e, dict)],
            "tests": dict(prior.get("tests") or {}) if isinstance(prior.get("tests"), dict) else {},
            "diff": str(prior.get("diff") or ""),
            "resumed": True,
        }
        return {
            "ok": True,
            "provider": "openhands-subprocess",
            "resumed": True,
            "workspace_id": resume_workspace_id,
            "diff": cp["diff"],
            "test_result": cp["tests"],
            "timeline": cp["timeline"],
            "codeplane": cp,
            "summary": "复跑：复用既有工作区，未重新执行引擎",
        }

    # --- Engine unavailable ⇒ explicit, non-silent degradation. -------------- #
    if not engine.available():
        reason = str(getattr(engine, "reason", "") or "代码执行引擎不可用")
        cp = {
            "engine": {
                "available": False,
                "interpreter": str(getattr(engine, "interpreter", "") or ""),
                "reason": reason,
            },
            "degraded": "engine_unavailable",
            "outcome": "degraded",
            "workspace": {},
            "timeline": [],
            "tests": {"measured": False, "verdict": "unmeasured"},
            "diff": "",
        }
        return {
            "ok": False,
            "outcome": "degraded",
            "unavailable": True,
            "provider": "openhands-subprocess",
            "degraded": "engine_unavailable",
            "reason": reason,
            "codeplane": cp,
            "workspace_id": "",
            "diff": "",
            "test_result": cp["tests"],
            "timeline": [],
            "summary": "代码执行引擎不可用，本次未执行任何代码改动",
        }

    # --- Real run: workspace (outside the project tree) → engine. ----------- #
    wm = get_workspace_manager()
    ws = wm.create(run_id, _codeplane_source(args), tenant_id=tenant_id)
    # INC27 §8/§9 — the enterprise context ForgeFlow selected for this run. Only
    # ever what the control plane selected; absent ⇒ empty (nothing was injected),
    # and never a block the handler invented.
    _skills_in = args.get("skill_context")
    _memory_in = args.get("memory_context")
    job = CodeJob(
        run_id=run_id,
        task_intent=intent,
        workspace_path=ws.path,
        language_hint=_text(args, "language_hint"),
        skill_context=[i for i in _skills_in if isinstance(i, dict)]
        if isinstance(_skills_in, list)
        else [],
        memory_context=[i for i in _memory_in if isinstance(i, dict)]
        if isinstance(_memory_in, list)
        else [],
    )
    result = await engine.run(job)
    tests_dict = result.tests.to_dict()
    # INC28 W4 — the explicit outcome. ``result.status == "ok"`` only means "the
    # engine ran without degrading"; it says nothing about whether anything was
    # changed or whether the tests pass. The extra signal is derived *mechanically*
    # from evidence ForgeFlow already owns (the workspace diff + the reviewer's
    # test verdict from ``codeplane/tests_verdict.py``).
    outcome = _derive_code_outcome(
        status=result.status, degraded=result.degraded,
        diff=result.diff, tests=tests_dict,
    )
    summary = _CODE_OUTCOME_SUMMARY.get(outcome, "代码任务执行完成")
    if outcome == "degraded":
        summary = f"代码任务执行降级：{result.degraded or result.status}"
    cp = {
        "engine": {
            "available": True,
            "interpreter": str(getattr(engine, "interpreter", "") or ""),
        },
        "degraded": result.degraded,
        "outcome": outcome,
        "workspace": ws.to_dict(),
        "timeline": [e.to_dict() for e in result.timeline],
        "tests": tests_dict,
        "diff": result.diff,
        "raw": dict(result.raw),
        # INC29 T02 (§6) — the progress-summary vocabulary
        # (files_changed / test_command / passed / failed / repair_rounds),
        # derived from real evidence in ``engine._parse``. Surfaced on the
        # codeplane payload so GET /runs/{id}.codeplane.summary carries it.
        "summary": dict(result.summary),
        # INC27 AC-3 / AC-6 — provenance for the audit story: exactly what the
        # platform injected (skills carry id + version + name, memory carries id).
        # Empty lists are the honest "nothing was injected", never a placeholder.
        "injected": {
            "skills": [
                {
                    "id": str(i.get("id") or ""),
                    "version": str(i.get("version") or ""),
                    "name": str(i.get("name") or ""),
                }
                for i in (job.skill_context or [])
                if isinstance(i, dict)
            ],
            "memory": [
                {
                    "id": str(i.get("id") or ""),
                    "scope": i.get("scope"),
                }
                for i in (job.memory_context or [])
                if isinstance(i, dict)
            ],
        },
    }
    payload: dict[str, Any] = {
        # ``ok`` is the platform's success bit: the code task really succeeded —
        # it produced a change AND the tests are not red. A run that changed
        # nothing (``no_change``), whose tests failed, or that degraded is NOT a
        # success (INC28 W4: the "engine ran" status must never be shown as 成功).
        "ok": outcome == "succeeded",
        "outcome": outcome,
        "provider": "openhands-subprocess",
        "workspace_id": ws.workspace_id,
        "diff": result.diff,
        "test_result": tests_dict,
        "timeline": cp["timeline"],
        "degraded": result.degraded,
        "codeplane": cp,
        "summary": summary,
    }
    if result.status == "unavailable":
        payload["unavailable"] = True
        payload["reason"] = str(result.raw.get("reason") or "代码执行引擎不可用")
    elif result.status == "error":
        payload["error"] = str(
            result.raw.get("reason") or result.degraded or "代码执行失败"
        )
    elif outcome != "succeeded":
        # A completed-but-unsuccessful run is a *business failure*, not a silent
        # success: name it so the executor records an honest ``error`` step.
        payload["error"] = summary
    return payload


def _workspace_diff(ws_path: str) -> str:
    """Best-effort ``git diff`` of the workspace against its baseline (``""``)."""
    if not ws_path:
        return ""
    import subprocess

    env = dict(os.environ)
    for key, value in _GIT_IDENT.items():
        env.setdefault(key, value)
    for argv in (["git", "-C", ws_path, "diff", "HEAD"], ["git", "-C", ws_path, "diff"]):
        try:
            completed = subprocess.run(
                argv, capture_output=True, text=True, timeout=30, check=False, env=env
            )
        except Exception:  # noqa: BLE001 — a diff is best-effort evidence
            return ""
        if completed.returncode == 0 and (completed.stdout or "").strip():
            return completed.stdout
    return ""


def _commit_workspace(ws_path: str, message: str) -> tuple[bool, str]:
    """Commit the workspace's changes onto its own branch (never pushed).

    Returns ``(committed, detail)``. The commit happens **inside the isolated
    workspace copy** — the target repository is never touched (AC-11 / AC-17).
    """
    if not ws_path:
        return False, "工作区路径不可用"
    import subprocess

    env = dict(os.environ)
    for key, value in _GIT_IDENT.items():
        env.setdefault(key, value)
    try:
        subprocess.run(
            ["git", "-C", ws_path, "add", "-A"],
            capture_output=True, text=True, timeout=30, check=False, env=env,
        )
        completed = subprocess.run(
            ["git", "-C", ws_path, "commit", "-m", message or "forgeflow: code task"],
            capture_output=True, text=True, timeout=30, check=False, env=env,
        )
    except FileNotFoundError:
        return False, "未找到 git 可执行文件"
    except Exception as exc:  # noqa: BLE001 — a commit failure is reported, not raised
        return False, f"提交失败：{exc}"
    if completed.returncode == 0:
        detail = (completed.stdout or "").strip().splitlines()
        return True, (detail[0][:400] if detail else "committed")
    return False, ((completed.stderr or completed.stdout or "").strip()[:400] or f"git commit exit {completed.returncode}")


#: How git reports "there is no uncommitted change" (en + zh-CN locales).
_NOTHING_TO_COMMIT_MARKERS: tuple[str, ...] = (
    "nothing to commit",
    "无文件要提交",
    "无需提交",
)

#: The subject prefix ``_commit_workspace`` writes (distinct from the workspace's
#: own baseline subject, which is ``"forgeflow workspace baseline"`` — no colon).
_FORGEFLOW_COMMIT_PREFIX = "forgeflow: "


def _is_nothing_to_commit(detail: str) -> bool:
    """Whether a failed ``git commit`` really means "the tree is already clean"."""
    low = (detail or "").lower()
    return any(marker.lower() in low for marker in _NOTHING_TO_COMMIT_MARKERS)


def _latest_forgeflow_commit(ws_path: str) -> str:
    """The subject of the workspace's most recent ``forgeflow:`` commit, else ``""``.

    Best-effort evidence for :func:`code_commit`'s idempotency: a ``forgeflow:``
    subject proves this flow already landed the approved change on the branch.
    The workspace's own baseline commit (``forgeflow workspace baseline``) is not
    a ``forgeflow:`` commit, so it is correctly ignored — an empty result means
    "no approved change was ever committed", never a fabricated one.
    """
    if not ws_path:
        return ""
    import subprocess

    env = dict(os.environ)
    for key, value in _GIT_IDENT.items():
        env.setdefault(key, value)
    try:
        completed = subprocess.run(
            ["git", "-C", ws_path, "log", "--format=%s", "-n", "10"],
            capture_output=True, text=True, timeout=20, check=False, env=env,
        )
    except Exception:  # noqa: BLE001 — idempotency evidence is best-effort
        return ""
    for subject in (completed.stdout or "").splitlines():
        subject = subject.strip()
        if subject.startswith(_FORGEFLOW_COMMIT_PREFIX):
            return subject
    return ""


async def code_commit(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """The human-in-the-loop gate over a code change (INC25 W2, §11 U2).

    Two shapes:

      * **No granted approval** (``args["approval"] != "approved"``) — the change is
        **not** committed. It returns ``{"ok": False, "awaiting_approval": True}`` so
        the executor records a **non-terminal, non-failure** ``awaiting_approval``
        step (``executed=False``, ``invoked=True``, ``latency_ms=None``) and the run
        ends ``status="awaiting_approval"`` (never "已完成", AC-17).
      * **Approved** — the workspace's changes are committed onto its **own branch**
        (never pushed to the target repository) and the diff + reviewed test evidence
        are returned so the artifact projection can raise ``code_diff`` /
        ``code_test_report`` deliverables (AC-20).

    The approval itself is recorded as an ``ApprovalRecord`` by
    :mod:`forgeflow.codeplane.approval`; this handler only reads the decision that
    was passed in — it never decides policy.
    """
    from forgeflow.codeplane.workspace import get_workspace_manager

    approval = str(args.get("approval") or "").strip().lower()
    workspace_id = str(args.get("workspace_id") or "").strip()
    prior = args.get("prior") if isinstance(args.get("prior"), dict) else {}
    diff_prior = str(prior.get("diff") or "")
    tests_prior = dict(prior.get("tests") or {}) if isinstance(prior.get("tests"), dict) else {}

    wm = get_workspace_manager()
    ws = wm.get(workspace_id) if workspace_id else None
    ws_dict = ws.to_dict() if ws is not None else dict(prior.get("workspace") or {})

    if approval != "approved":
        cp = {
            "approval": {"status": "pending"},
            "workspace": ws_dict,
            "diff": diff_prior,
            "tests": tests_prior,
        }
        return {
            "ok": False,
            "awaiting_approval": True,
            "provider": "workspace-git",
            "workspace_id": workspace_id,
            "diff": diff_prior,
            "test_result": tests_prior,
            "codeplane": cp,
            "reason": "代码变更等待人工审批；未批准前不写入工作区分支",
            "summary": "等待人工审批",
        }

    intent = _text(args, "intent") or str(getattr(ctx, "intent", "") or "")
    # Capture the approved change's diff BEFORE committing: once the commit
    # succeeds the tree is clean and ``git diff`` is empty, so the deliverable
    # would otherwise survive only via the carried-forward ``diff_prior``.
    diff = _workspace_diff(ws.path) if (ws is not None and ws.path) else ""
    commit_ok, commit_detail = (False, "工作区路径不可用")
    if ws is not None and ws.path:
        commit_ok, commit_detail = _commit_workspace(ws.path, f"forgeflow: {intent[:60]}")
        if not commit_ok and _is_nothing_to_commit(commit_detail):
            # INC25 P0-B — idempotency. "nothing to commit" means the tree has no
            # *uncommitted* change; if this flow already landed the approved change
            # (a replan re-invoked code.commit, or /approve raced a resume), the
            # postcondition HOLDS and reporting an error would be a false negative.
            # Only claim it when a real ``forgeflow:`` commit proves it — an
            # untouched workspace (baseline only) still reports committed=False.
            prior_subject = _latest_forgeflow_commit(ws.path)
            if prior_subject:
                commit_ok, commit_detail = True, prior_subject
    if not diff:
        diff = diff_prior
    cp = {
        "approval": {"status": "approved"},
        "workspace": ws_dict,
        "diff": diff,
        "tests": tests_prior,
        "committed": bool(commit_ok),
        "commit_detail": commit_detail,
    }
    payload: dict[str, Any] = {
        "ok": bool(commit_ok),
        "provider": "workspace-git",
        "workspace_id": workspace_id,
        "diff": diff,
        "test_result": tests_prior,
        "committed": bool(commit_ok),
        "codeplane": cp,
        "summary": (
            "代码变更已提交到工作区分支" if commit_ok else f"提交未完成：{commit_detail}"
        ),
    }
    if not commit_ok:
        payload["error"] = commit_detail
    return payload


# --------------------------------------------------------------------------- #
# INC43 S4 — the DOCX document-editing plane                                    #
#                                                                               #
# Layering (design §1.1 / §1.3): the LLM only produces an *edit intent*; the    #
# Tool layer (``document.edit``) really writes the new DOCX bytes with          #
# ``python-docx``. ``document.inspect`` reports the real structure and          #
# ``artifact.save`` registers the produced deliverable. The bytes never enter   #
# the run payload (see ``forgeflow.documents.store``).                          #
# --------------------------------------------------------------------------- #
#: The editable Office document suffixes (INC44 §1.2 — DOCX + PPTX).
_DOCUMENT_SUFFIXES: tuple[str, ...] = (".docx", ".pptx")


def _first_docx_path(paths: list[str]) -> str:
    """First real Office document (``.docx`` / ``.pptx``) in ``paths`` (``""`` else)."""
    for raw in paths:
        text = str(raw or "").strip()
        if text.lower().endswith(_DOCUMENT_SUFFIXES) and os.path.isfile(text):
            return text
    return ""


def _document_target(args: dict[str, Any]) -> str:
    """The real Office document the document tools operate on (``""`` when none).

    Prefers the resource seam's ``document_paths`` (INC43 T04-fix): a registered
    ``.docx`` / ``.pptx`` FILE dereferences to a real, content-addressed path that
    carries **no** extension, so :func:`_first_docx_path`'s suffix check would
    never see it. Falls back to the first ``.docx`` / ``.pptx``-named path in
    ``paths`` / ``repo_path`` for the caller's explicit-path case. A candidate is
    accepted only when it is a real file — the target is never guessed.
    """
    raw = args.get("document_paths")
    candidates: list[str] = []
    if isinstance(raw, (list, tuple)):
        candidates = [str(p).strip() for p in raw if str(p or "").strip()]
    elif isinstance(raw, str) and raw.strip():
        candidates = [raw.strip()]
    for text in candidates:
        if os.path.isfile(text):
            return text
    return _first_docx_path(_path_list(args))


def _sniff_ooxml(data: bytes) -> str:
    """Sniff an OOXML package's real format from its zip members.

    A registered document path is content-addressed (extensionless), so the
    format **must** be read from the bytes: ``ppt/`` ⇒ ``"pptx"``, ``word/`` ⇒
    ``"docx"``. Anything that is not a readable OOXML package ⇒ ``""``.
    """
    try:
        import io as _io
        import zipfile

        with zipfile.ZipFile(_io.BytesIO(bytes(data))) as archive:
            names = archive.namelist()
    except Exception:  # noqa: BLE001 — a non-zip blob simply has no format
        return ""
    if any(name.startswith("ppt/") for name in names):
        return "pptx"
    if any(name.startswith("word/") for name in names):
        return "docx"
    return ""


def _edited_filename(source: str) -> str:
    """The new DOCX's name (base name only): ``report.docx`` → ``report.edited.docx``."""
    base = os.path.basename(str(source or "").strip()) or "document.docx"
    stem = base[:-5] if base.lower().endswith(".docx") else base
    return f"{stem}.edited.docx"


def _safe_docx_stem(name: str) -> str:
    """A directory-free ``.docx`` stem for a deliverable name (traversal-safe).

    Keeps only the **final path component** (splitting on both ``/`` and ``\\``),
    rejects ``.`` / ``..`` components, and drops any residual separator — so the
    produced name can **never** carry a directory. Returns ``""`` when nothing
    safe remains (the caller then falls back to the real source path's name).
    """
    base = str(name or "").replace("\\", "/").split("/")[-1].strip()
    if base in ("", ".", ".."):
        return ""
    stem = base[:-5] if base.lower().endswith(".docx") else base
    stem = stem.strip().strip(".").strip()
    if stem in ("", ".", "..") or "/" in stem or "\\" in stem:
        return ""
    return stem


def _safe_doc_stem(name: str, ext: str) -> str:
    """A directory-free, extension-stripped stem for ``ext`` (traversal-safe).

    The generic form of :func:`_safe_docx_stem` (INC44 §1.2/§1.3): used for PPTX
    (``ext=".pptx"``) and text deliverables too. Returns ``""`` when nothing safe
    remains.
    """
    suffix = ext.lower()
    base = str(name or "").replace("\\", "/").split("/")[-1].strip()
    if base in ("", ".", ".."):
        return ""
    stem = base[: -len(suffix)] if base.lower().endswith(suffix) else base
    stem = stem.strip().strip(".").strip()
    if stem in ("", ".", "..") or "/" in stem or "\\" in stem:
        return ""
    return stem


def _document_filename(args: dict[str, Any], target: str) -> str:
    """The produced DOCX deliverable's name — the registered original, when known.

    The resource seam passes ``document_names`` index-aligned with
    ``document_paths`` (INC43 T04-fix). When the target's slot carries a real
    name, the deliverable uses that name's sanitized stem (so the user sees
    ``报告.edited.docx`` rather than the content-addressed path's hash); otherwise
    it falls back to :func:`_edited_filename` (the real source path's base name).
    A name is used **only** when it is real and safe — never invented, and never
    allowed to carry a directory (see :func:`_safe_docx_stem`).
    """
    document_paths = args.get("document_paths")
    document_names = args.get("document_names")
    if isinstance(document_paths, (list, tuple)) and isinstance(document_names, (list, tuple)):
        wanted = str(target or "").strip()
        for idx, raw in enumerate(document_paths):
            if str(raw or "").strip() != wanted:
                continue
            if idx < len(document_names):
                stem = _safe_doc_stem(document_names[idx], ".docx")
                if stem:
                    return f"{stem}.edited.docx"
            break
    return _edited_filename(target)


def _presentation_filename(args: dict[str, Any], target: str) -> str:
    """The produced PPTX deliverable's name — the registered original, when known.

    Mirrors :func:`_document_filename` for ``.pptx`` (``报告.pptx`` →
    ``报告.edited.pptx``), sanitized and traversal-safe (see
    :func:`_safe_doc_stem`).
    """
    document_paths = args.get("document_paths")
    document_names = args.get("document_names")
    if isinstance(document_paths, (list, tuple)) and isinstance(document_names, (list, tuple)):
        wanted = str(target or "").strip()
        for idx, raw in enumerate(document_paths):
            if str(raw or "").strip() != wanted:
                continue
            if idx < len(document_names):
                stem = _safe_doc_stem(document_names[idx], ".pptx")
                if stem:
                    return f"{stem}.edited.pptx"
            break
    base = os.path.basename(str(target or "").strip()) or "presentation.pptx"
    stem = base[:-5] if base.lower().endswith(".pptx") else base
    return f"{stem}.edited.pptx"


def _textfile_target(args: dict[str, Any]) -> str:
    """The real text / code file the text tools operate on (``""`` when none).

    Prefers the resource seam's ``text_paths`` (INC44 §1.3): a registered text /
    code FILE dereferences to a real, content-addressed path that carries **no**
    extension, so a suffix scan would never see it. Falls back to the first real
    file in ``paths`` whose suffix is a registered text / code extension. A
    candidate is accepted only when it is a real file — never guessed.
    """
    from forgeflow.resources.summaries import TEXT_EXTENSIONS

    raw = args.get("text_paths")
    candidates: list[str] = []
    if isinstance(raw, (list, tuple)):
        candidates = [str(p).strip() for p in raw if str(p or "").strip()]
    elif isinstance(raw, str) and raw.strip():
        candidates = [raw.strip()]
    for text in candidates:
        if os.path.isfile(text):
            return text
    text_exts = set(TEXT_EXTENSIONS)
    for path in _path_list(args):
        if os.path.isfile(path) and os.path.splitext(path)[1].lower() in text_exts:
            return path
    return ""


def _textfile_filename(args: dict[str, Any], target: str) -> str:
    """The produced text deliverable's name — the registered original, when known.

    Mirrors :func:`_document_filename`: the delivered name keeps the user's real
    file name (``app.py`` → ``app.edited.py``) and is sanitized so it can never
    carry a directory. Falls back to the solver's real source-path name.
    """
    text_paths = args.get("text_paths")
    text_names = args.get("text_names")
    if isinstance(text_paths, (list, tuple)) and isinstance(text_names, (list, tuple)):
        wanted = str(target or "").strip()
        for idx, raw in enumerate(text_paths):
            if str(raw or "").strip() != wanted:
                continue
            if idx < len(text_names):
                base = str(text_names[idx] or "").replace("\\", "/").split("/")[-1].strip()
                stem, ext = os.path.splitext(base)
                stem = stem.strip().strip(".")
                if stem and "/" not in stem and "\\" not in stem:
                    return f"{stem}.edited{ext or '.txt'}"
            break
    base = os.path.basename(str(target or "").strip()) or "file.txt"
    stem, ext = os.path.splitext(base)
    return f"{stem or 'file'}.edited{ext or '.txt'}"


# --------------------------------------------------------------------------- #
# INC45 §1.1/§1.2 — XLSX + PDF plane targets / names / constraints             #
# --------------------------------------------------------------------------- #
_SHEET_SUFFIXES: tuple[str, ...] = (".xlsx", ".xlsm")
_PDF_SUFFIX = ".pdf"


def _first_sheet_path(paths: list[str]) -> str:
    """First real workbook (``.xlsx`` / ``.xlsm``) in ``paths`` (``""`` else)."""
    for raw in paths:
        text = str(raw or "").strip()
        if text.lower().endswith(_SHEET_SUFFIXES) and os.path.isfile(text):
            return text
    return ""


def _sheet_target(args: dict[str, Any]) -> str:
    """The real workbook the sheet tools operate on (``""`` when none).

    Prefers the resource seam's ``sheet_paths`` (INC45 §1.3): a registered
    ``.xlsx`` FILE dereferences to a real, content-addressed path that carries
    **no** extension, so a suffix scan would never see it. Falls back to the
    first ``.xlsx`` / ``.xlsm``-named path in ``paths`` / ``repo_path``. A
    candidate is accepted only when it is a real file — the target is never
    guessed.
    """
    raw = args.get("sheet_paths")
    candidates: list[str] = []
    if isinstance(raw, (list, tuple)):
        candidates = [str(p).strip() for p in raw if str(p or "").strip()]
    elif isinstance(raw, str) and raw.strip():
        candidates = [raw.strip()]
    for text in candidates:
        if os.path.isfile(text):
            return text
    return _first_sheet_path(_path_list(args))


def _sheet_filename(args: dict[str, Any], target: str) -> str:
    """The produced workbook deliverable's name — the registered original if known.

    Mirrors :func:`_document_filename`: ``book.xlsx`` → ``book.edited.xlsx``,
    sanitized and traversal-safe (see :func:`_safe_doc_stem`).
    """
    sheet_paths = args.get("sheet_paths")
    sheet_names = args.get("sheet_names")
    if isinstance(sheet_paths, (list, tuple)) and isinstance(sheet_names, (list, tuple)):
        wanted = str(target or "").strip()
        for idx, raw in enumerate(sheet_paths):
            if str(raw or "").strip() != wanted:
                continue
            if idx < len(sheet_names):
                stem = _safe_doc_stem(sheet_names[idx], ".xlsx")
                if stem:
                    return f"{stem}.edited.xlsx"
            break
    base = os.path.basename(str(target or "").strip()) or "book.xlsx"
    stem = base[:-5] if base.lower().endswith(".xlsx") else base
    return f"{stem}.edited.xlsx"


def _first_pdf_path(paths: list[str]) -> str:
    """First real ``.pdf`` in ``paths`` (``""`` when none)."""
    for raw in paths:
        text = str(raw or "").strip()
        if text.lower().endswith(_PDF_SUFFIX) and os.path.isfile(text):
            return text
    return ""


def _pdf_target(args: dict[str, Any]) -> str:
    """The real PDF the pdf tools operate on (``""`` when none).

    Prefers the resource seam's ``pdf_paths`` (INC45 §1.3) — a registered ``.pdf``
    FILE dereferences to a content-addressed (extensionless) path — then falls back
    to the first ``.pdf``-named path in ``paths`` / ``repo_path``. Never guessed.
    """
    raw = args.get("pdf_paths")
    candidates: list[str] = []
    if isinstance(raw, (list, tuple)):
        candidates = [str(p).strip() for p in raw if str(p or "").strip()]
    elif isinstance(raw, str) and raw.strip():
        candidates = [raw.strip()]
    for text in candidates:
        if os.path.isfile(text):
            return text
    return _first_pdf_path(_path_list(args))


def _pdf_filename(args: dict[str, Any], target: str = "") -> str:
    """The produced PDF deliverable's name — the registered original if known.

    ``report.pdf`` → ``report.generated.pdf`` (D7). ``pdf.generate`` does not read
    the source bytes (D6), so the name is derived from the declared ``pdf_names``
    when present, else the target's base name, else a neutral default; it is
    sanitized so it can never carry a directory.
    """
    pdf_paths = args.get("pdf_paths")
    pdf_names = args.get("pdf_names")
    names_list = list(pdf_names) if isinstance(pdf_names, (list, tuple)) else []
    wanted = str(target or "").strip()
    if isinstance(pdf_paths, (list, tuple)) and names_list:
        for idx, raw in enumerate(pdf_paths):
            if str(raw or "").strip() != wanted:
                continue
            if idx < len(names_list):
                stem = _safe_doc_stem(names_list[idx], ".pdf")
                if stem:
                    return f"{stem}.generated.pdf"
            break
    if names_list:
        stem = _safe_doc_stem(names_list[0], ".pdf")
        if stem:
            return f"{stem}.generated.pdf"
    base = os.path.basename(str(target or "").strip())
    if base:
        stem = base[:-4] if base.lower().endswith(".pdf") else base
        stem = stem.strip().strip(".")
        if stem and "/" not in stem and "\\" not in stem:
            return f"{stem}.generated.pdf"
    return "document.generated.pdf"


def _sheet_edit_constraints(
    args: dict[str, Any], structure: Any, data: bytes, ops: list[Any]
) -> dict[str, Any]:
    """The honest constraint bag the sheet edit handler verifies against.

    The worksheet count of the ORIGINAL must be preserved; the original numeric
    tokens must survive unless an explicit edit legitimately liberated them
    (``sheet_edit.numbers_removable_by_edits``). Any caller-supplied ``max_chars`` /
    ``min_chars`` passes through unchanged.
    """
    from forgeflow.documents import sheet_numbers, sheet_numbers_removable_by_edits

    constraints: dict[str, Any] = {
        "expected_sheets": len(structure.sheets),
        "original_numbers": sheet_numbers(data),
        "allowed_missing_numbers": sorted(sheet_numbers_removable_by_edits(data, ops)),
    }
    for key in ("max_chars", "min_chars"):
        value = args.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            constraints[key] = int(value)
    return constraints


def _pdf_generate_constraints(args: dict[str, Any]) -> dict[str, Any]:
    """The honest constraint bag ``pdf.generate`` verifies its output against.

    Only constraints the caller really supplied are set (``expected_pages`` /
    ``max_chars`` / ``min_chars`` / ``must_contain``); everything else stays
    unmeasured (``None``). ``verify_pdf`` still reports ``openable`` truthfully, so
    a PDF that cannot be re-parsed fails the check.
    """
    constraints: dict[str, Any] = {}
    for key in ("expected_pages", "max_chars", "min_chars"):
        value = args.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            constraints[key] = int(value)
    must_contain = args.get("must_contain")
    if isinstance(must_contain, (list, tuple)) and must_contain:
        constraints["must_contain"] = [str(token) for token in must_contain]
    return constraints


def _doc_edit_constraints(
    args: dict[str, Any], structure: Any, data: bytes, ops: list[Any]
) -> dict[str, Any]:
    """The honest constraint bag the edit handler verifies the result against.

    Structure (section / table counts of the ORIGINAL) must be preserved; the
    original numeric tokens must survive unless an explicit edit legitimately
    liberated them (``numbers_removable_by_edits``). Any caller-supplied
    ``max_chars`` / ``min_chars`` is passed through unchanged.
    """
    from forgeflow.documents import document_numbers, numbers_removable_by_edits

    constraints: dict[str, Any] = {
        "expected_sections": len(structure.sections),
        "expected_tables": structure.tables,
        "original_numbers": document_numbers(data),
        "allowed_missing_numbers": sorted(numbers_removable_by_edits(data, ops)),
    }
    for key in ("max_chars", "min_chars"):
        value = args.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            constraints[key] = int(value)
    return constraints


def _report_passed(report: Any) -> bool:
    """A verification passes unless a **measured** dimension is ``False``.

    ``None`` means "not measured" (no constraint for it) and is never treated as
    a failure — the honest tri-state rule from ``documents.validation``.
    """
    return (
        bool(report.openable)
        and report.structure_ok is not False
        and report.data_ok is not False
        and report.requirement_ok is not False
    )


def _pptx_edit_constraints(
    args: dict[str, Any], structure: Any, data: bytes, ops: list[Any]
) -> dict[str, Any]:
    """The honest constraint bag the PPTX edit handler verifies against.

    The slide count of the ORIGINAL must be preserved; the original numeric
    tokens must survive unless an explicit edit legitimately liberated them
    (``pptx_edit.numbers_removable_by_edits``). Any caller-supplied ``max_chars`` /
    ``min_chars`` is passed through unchanged.
    """
    from forgeflow.documents.pptx_edit import numbers_removable_by_edits
    from forgeflow.documents.pptx_inspect import presentation_numbers

    constraints: dict[str, Any] = {
        "expected_slides": structure.slides,
        "original_numbers": presentation_numbers(data),
        "allowed_missing_numbers": sorted(numbers_removable_by_edits(data, ops)),
    }
    for key in ("max_chars", "min_chars"):
        value = args.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            constraints[key] = int(value)
    return constraints


def _textfile_edit_constraints(
    args: dict[str, Any], target: str, data: bytes, ops: list[Any]
) -> dict[str, Any]:
    """The honest constraint bag the text edit handler verifies against.

    Only constraints the caller really supplied (or that are truthfully
    derivable) are set: a ``.py`` target gets the "still compilable Python" check
    (``expect_compilable_python``); ``max_chars`` / ``min_chars`` / ``must_contain``
    pass through unchanged. Everything else stays unmeasured (``None``).
    """
    constraints: dict[str, Any] = {}
    explicit_py = args.get("expect_compilable_python")
    if explicit_py is True or (
        explicit_py is None and str(target or "").lower().endswith(".py")
    ):
        constraints["expect_compilable_python"] = True
    must_contain = args.get("must_contain")
    if isinstance(must_contain, (list, tuple)) and must_contain:
        constraints["must_contain"] = [str(token) for token in must_contain]
    for key in ("max_chars", "min_chars"):
        value = args.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            constraints[key] = int(value)
    return constraints


async def document_inspect(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Read a real Office document's structure (DOCX or PPTX). Never invents facts.

    The format is **sniffed from the bytes** (a registered document path is
    content-addressed and extensionless), so ``.docx`` is read with
    ``python-docx`` and ``.pptx`` with ``python-pptx``. A missing / non-Office
    path, an unreadable file or an unparseable document returns ``not_executed``
    (the executor records ``blocked``) with the real reason — this handler never
    reports a structure it did not measure.
    """
    target = _document_target(args)
    if not target:
        return {
            "ok": False,
            "not_executed": True,
            "provider": "python-docx",
            "reason": "未提供可读取的 .docx/.pptx 文件路径（document_paths/paths 缺失或非 docx），未执行",
        }
    try:
        with open(target, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        return {
            "ok": False,
            "not_executed": True,
            "provider": "python-docx",
            "file": target,
            "reason": f"文档不可读取：{exc}",
        }

    fmt = _sniff_ooxml(data)
    if fmt == "pptx":
        from forgeflow.documents import PptxInspectionError, inspect_pptx

        try:
            pptx_structure = inspect_pptx(data)
        except PptxInspectionError as exc:
            return {
                "ok": False,
                "not_executed": True,
                "provider": "python-pptx",
                "file": target,
                "reason": f"PPTX 解析失败：{exc}",
            }
        return {
            "ok": True,
            "provider": "python-pptx",
            "file": target,
            "format": "pptx",
            "structure": pptx_structure.to_dict(limit=60),
            "summary": (
                f"inspect：{pptx_structure.slides} 幻灯片，{pptx_structure.shapes} 形状，"
                f"{pptx_structure.text_frames} 文本框，{pptx_structure.charts} 图表"
            ),
            "result_ref": f"pptx-inspect:{os.path.basename(target)}:{len(data)}",
        }

    from forgeflow.documents import DocxInspectionError, inspect_docx

    try:
        structure = inspect_docx(data)
    except DocxInspectionError as exc:
        return {
            "ok": False,
            "not_executed": True,
            "provider": "python-docx",
            "file": target,
            "reason": f"DOCX 解析失败：{exc}",
        }
    return {
        "ok": True,
        "provider": "python-docx",
        "file": target,
        "format": "docx",
        "structure": structure.to_dict(limit=60),
        "summary": (
            f"inspect：{structure.paragraphs} 段落，{len(structure.headings)} 标题，"
            f"{structure.tables} 表格，{structure.words} 字"
        ),
        "result_ref": f"docx-inspect:{os.path.basename(target)}:{len(data)}",
    }


async def _resolve_edit_ops(
    args: dict[str, Any],
    data: bytes,
    structure: Any,
    *,
    op_from_dict: Any,
    unknown_error: type[Exception],
    resolve_intent: Any,
    provider: str,
    target: str,
) -> tuple[list[Any] | None, bool, dict[str, Any] | None]:
    """Resolve the edit intent: explicit ``args["edits"]`` first, else the LLM.

    Returns ``(ops, from_intent, early_error)``. ``early_error`` is a ready-made
    honest-failure payload when the caller supplied an illegal op or no intent
    could be resolved (never a fabricated success).
    """
    raw_edits = args.get("edits")
    ops: list[Any] | None = None
    if isinstance(raw_edits, (list, tuple)) and raw_edits:
        try:
            ops = [op_from_dict(item) for item in raw_edits]
        except unknown_error as exc:
            return None, False, {
                "ok": False,
                "provider": provider,
                "file": target,
                "error": str(exc),
                "reason": f"编辑意图非法：{exc}",
            }
    from_intent = ops is None
    if from_intent:
        intent = _text(args, "intent", "text")
        ops = await resolve_intent(data, intent, structure)
        if not ops:
            return None, True, {
                "ok": False,
                "unavailable": True,
                "provider": provider,
                "file": target,
                "reason": "缺少编辑意图(edits)且未连接模型服务，未修改（不伪造成功）",
            }
    return ops, from_intent, None


async def document_edit(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """The document Tool layer: apply an edit intent and **really write** bytes.

    Dispatches by the sniffed format: ``.docx`` → ``python-docx``,
    ``.pptx`` → ``python-pptx``. Resolution order for the edit intent:

      1. ``args["edits"]`` — the caller's explicit op array (used verbatim);
      2. otherwise ``documents.resolve_intent`` — the LLM layer turns the intent
         text into ops (never writes bytes itself);
      3. neither ⇒ an **honest failure** (``unavailable``), never a fabricated
         "已修改 N 处".

    On success the new bytes are persisted to the ``DocArtifactStore`` (outside
    the project tree) and the returned payload carries only the small summary +
    ``artifact_ref`` — the bytes never enter the bounded run payload. A failed
    verification is retried (≤2 extra attempts, re-resolving the intent) and then
    reported honestly.
    """
    target = _document_target(args)
    if not target:
        return {
            "ok": False,
            "not_executed": True,
            "provider": "python-docx",
            "reason": "未提供可读取的 .docx/.pptx 文件路径（document_paths/paths 缺失或非 docx），未执行（不猜测文档）",
        }
    try:
        with open(target, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        return {
            "ok": False,
            "not_executed": True,
            "provider": "python-docx",
            "file": target,
            "reason": f"文档不可读取：{exc}",
        }
    if _sniff_ooxml(data) == "pptx":
        return await _edit_presentation(args, target, data)
    return await _edit_docx(args, target, data)


async def _edit_docx(args: dict[str, Any], target: str, data: bytes) -> dict[str, Any]:
    """The DOCX Tool layer (python-docx writer) — byte-for-byte its INC43 behaviour."""
    from forgeflow.documents import (
        DocArtifactStore,
        DocxInspectionError,
        EditOp,
        UnknownEditOpError,
        apply_edits,
        compute_diff,
        inspect_docx,
        resolve_intent,
        verify_docx,
    )

    try:
        structure = inspect_docx(data)
    except DocxInspectionError as exc:
        return {
            "ok": False,
            "not_executed": True,
            "provider": "python-docx",
            "file": target,
            "reason": f"DOCX 解析失败：{exc}",
        }

    ops, from_intent, early = await _resolve_edit_ops(
        args,
        data,
        structure,
        op_from_dict=EditOp.from_dict,
        unknown_error=UnknownEditOpError,
        resolve_intent=resolve_intent,
        provider="python-docx",
        target=target,
    )
    if early is not None:
        return early
    assert ops is not None

    constraints = _doc_edit_constraints(args, structure, data, ops)
    new_bytes, changes = apply_edits(data, ops)
    report = verify_docx(new_bytes, constraints)
    max_attempts = 3 if from_intent else 1
    attempts = 0
    while not _report_passed(report) and attempts < max_attempts - 1:
        attempts += 1
        retry_ops = await resolve_intent(data, _text(args, "intent", "text"), structure)
        if not retry_ops:
            break
        new_bytes, changes = apply_edits(data, retry_ops)
        report = verify_docx(new_bytes, constraints)
    if not _report_passed(report):
        return {
            "ok": False,
            "provider": "python-docx",
            "file": target,
            "modified": 0,
            "added": 0,
            "removed": 0,
            "numeric_changes": 0,
            "validation": report.to_dict(),
            "reason": (
                f"校验未通过（重试 ≤{max_attempts - 1} 次后仍未满足约束）："
                f"{report.notes or '未知原因'}，未产出文档"
            ),
        }

    diff = compute_diff(data, new_bytes)
    try:
        artifact_ref = DocArtifactStore().put(new_bytes, ".docx")
    except Exception as exc:  # noqa: BLE001 — a store failure must be reported, not hidden
        return {
            "ok": False,
            "provider": "python-docx",
            "file": target,
            "validation": report.to_dict(),
            "reason": f"文档产物落盘失败：{exc}",
        }

    filename = _document_filename(args, target)
    return {
        "ok": True,
        "provider": "python-docx",
        "file": target,
        "format": "docx",
        "filename": filename,
        "modified": diff.modified,
        "added": diff.added,
        "removed": diff.removed,
        "numeric_changes": diff.numeric_changes,
        "changes": changes,
        "artifact_ref": artifact_ref,
        "size_bytes": len(new_bytes),
        "validation": report.to_dict(),
        "summary": (
            f"已修改文档：{filename}（修改 {diff.modified}｜新增 {diff.added}｜"
            f"删除 {diff.removed}｜数字变化 {diff.numeric_changes}）"
        ),
        "result_ref": artifact_ref,
    }


async def _edit_presentation(args: dict[str, Any], target: str, data: bytes) -> dict[str, Any]:
    """The PPTX Tool layer (python-pptx writer) — the DOCX handler's mirror."""
    from forgeflow.documents import (
        DocArtifactStore,
        PptxEditOp,
        PptxInspectionError,
        UnknownEditOpError,
        apply_pptx_edits,
        compute_pptx_diff,
        inspect_pptx,
        resolve_pptx_intent,
        verify_pptx,
    )

    try:
        structure = inspect_pptx(data)
    except PptxInspectionError as exc:
        return {
            "ok": False,
            "not_executed": True,
            "provider": "python-pptx",
            "file": target,
            "reason": f"PPTX 解析失败：{exc}",
        }

    ops, from_intent, early = await _resolve_edit_ops(
        args,
        data,
        structure,
        op_from_dict=PptxEditOp.from_dict,
        unknown_error=UnknownEditOpError,
        resolve_intent=resolve_pptx_intent,
        provider="python-pptx",
        target=target,
    )
    if early is not None:
        return early
    assert ops is not None

    constraints = _pptx_edit_constraints(args, structure, data, ops)
    new_bytes, changes = apply_pptx_edits(data, ops)
    report = verify_pptx(new_bytes, constraints)
    max_attempts = 3 if from_intent else 1
    attempts = 0
    while not _report_passed(report) and attempts < max_attempts - 1:
        attempts += 1
        retry_ops = await resolve_pptx_intent(data, _text(args, "intent", "text"), structure)
        if not retry_ops:
            break
        new_bytes, changes = apply_pptx_edits(data, retry_ops)
        report = verify_pptx(new_bytes, constraints)
    if not _report_passed(report):
        return {
            "ok": False,
            "provider": "python-pptx",
            "file": target,
            "modified": 0,
            "added": 0,
            "removed": 0,
            "numeric_changes": 0,
            "validation": report.to_dict(),
            "reason": (
                f"校验未通过（重试 ≤{max_attempts - 1} 次后仍未满足约束）："
                f"{report.notes or '未知原因'}，未产出演示文稿"
            ),
        }

    diff = compute_pptx_diff(data, new_bytes)
    try:
        artifact_ref = DocArtifactStore().put(new_bytes, ".pptx")
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "provider": "python-pptx",
            "file": target,
            "validation": report.to_dict(),
            "reason": f"文档产物落盘失败：{exc}",
        }

    filename = _presentation_filename(args, target)
    return {
        "ok": True,
        "provider": "python-pptx",
        "file": target,
        "format": "pptx",
        "filename": filename,
        "modified": diff.modified,
        "added": diff.added,
        "removed": diff.removed,
        "numeric_changes": diff.numeric_changes,
        "changes": changes,
        "artifact_ref": artifact_ref,
        "size_bytes": len(new_bytes),
        "validation": report.to_dict(),
        "summary": (
            f"已修改演示文稿：{filename}（修改 {diff.modified}｜新增 {diff.added}｜"
            f"删除 {diff.removed}｜数字变化 {diff.numeric_changes}）"
        ),
        "result_ref": artifact_ref,
    }


async def textfile_inspect(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Read a real text / code file's structure (stdlib). Never invents facts.

    A missing / non-text path or an unreadable file returns ``not_executed`` (the
    executor records ``blocked``) with the real reason.
    """
    from forgeflow.documents import TextInspectionError, inspect_textfile

    target = _textfile_target(args)
    if not target:
        return {
            "ok": False,
            "not_executed": True,
            "provider": "stdlib",
            "reason": "未提供可读取的文本/代码文件路径（text_paths/paths 缺失或非文本），未执行",
        }
    try:
        with open(target, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        return {
            "ok": False,
            "not_executed": True,
            "provider": "stdlib",
            "file": target,
            "reason": f"文本文件不可读取：{exc}",
        }
    try:
        structure = inspect_textfile(data)
    except TextInspectionError as exc:
        return {
            "ok": False,
            "not_executed": True,
            "provider": "stdlib",
            "file": target,
            "reason": f"文本文件解析失败：{exc}",
        }
    return {
        "ok": True,
        "provider": "stdlib",
        "file": target,
        "format": "text",
        "structure": structure.to_dict(),
        "summary": (
            f"inspect：{structure.lines} 行，编码 {structure.encoding}，"
            f"换行 {structure.eol}，BOM {'有' if structure.has_bom else '无'}"
        ),
        "result_ref": f"text-inspect:{os.path.basename(target)}:{len(data)}",
    }


async def textfile_edit(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """The text / code Tool layer: apply an edit intent and **really write** bytes.

    Resolution order mirrors ``document.edit``: explicit ``args["edits"]`` first,
    else the LLM layer. On success the new bytes are persisted to the
    ``DocArtifactStore`` (``.txt``) with the payload carrying only the small
    summary + ``artifact_ref``. The file's EOL style / encoding / BOM are
    preserved by ``apply_edits``; a failed verification is retried (bounded) and
    then reported honestly.
    """
    from forgeflow.documents import (
        DocArtifactStore,
        TextEditOp,
        TextInspectionError,
        UnknownEditOpError,
        apply_textfile_edits,
        compute_textfile_diff,
        inspect_textfile,
        resolve_textfile_intent,
        verify_textfile,
    )

    target = _textfile_target(args)
    if not target:
        return {
            "ok": False,
            "not_executed": True,
            "provider": "stdlib",
            "reason": "未提供可读取的文本/代码文件路径（text_paths/paths 缺失或非文本），未执行（不猜测文件）",
        }
    try:
        with open(target, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        return {
            "ok": False,
            "not_executed": True,
            "provider": "stdlib",
            "file": target,
            "reason": f"文本文件不可读取：{exc}",
        }
    try:
        structure = inspect_textfile(data)
    except TextInspectionError as exc:
        return {
            "ok": False,
            "not_executed": True,
            "provider": "stdlib",
            "file": target,
            "reason": f"文本文件解析失败：{exc}",
        }

    ops, from_intent, early = await _resolve_edit_ops(
        args,
        data,
        structure,
        op_from_dict=TextEditOp.from_dict,
        unknown_error=UnknownEditOpError,
        resolve_intent=resolve_textfile_intent,
        provider="stdlib",
        target=target,
    )
    if early is not None:
        return early
    assert ops is not None

    constraints = _textfile_edit_constraints(args, target, data, ops)
    new_bytes, changes = apply_textfile_edits(data, ops, filename=target)
    report = verify_textfile(new_bytes, constraints)
    max_attempts = 3 if from_intent else 1
    attempts = 0
    while not _report_passed(report) and attempts < max_attempts - 1:
        attempts += 1
        retry_ops = await resolve_textfile_intent(
            data, _text(args, "intent", "text"), structure
        )
        if not retry_ops:
            break
        new_bytes, changes = apply_textfile_edits(data, retry_ops, filename=target)
        report = verify_textfile(new_bytes, constraints)
    if not _report_passed(report):
        return {
            "ok": False,
            "provider": "stdlib",
            "file": target,
            "modified": 0,
            "added": 0,
            "removed": 0,
            "numeric_changes": 0,
            "validation": report.to_dict(),
            "reason": (
                f"校验未通过（重试 ≤{max_attempts - 1} 次后仍未满足约束）："
                f"{report.notes or '未知原因'}，未产出文件"
            ),
        }

    diff = compute_textfile_diff(data, new_bytes)
    try:
        artifact_ref = DocArtifactStore().put(new_bytes, ".txt")
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "provider": "stdlib",
            "file": target,
            "validation": report.to_dict(),
            "reason": f"文件产物落盘失败：{exc}",
        }

    filename = _textfile_filename(args, target)
    return {
        "ok": True,
        "provider": "stdlib",
        "file": target,
        "format": "text",
        "filename": filename,
        "modified": diff.modified,
        "added": diff.added,
        "removed": diff.removed,
        "numeric_changes": diff.numeric_changes,
        "changes": changes,
        "artifact_ref": artifact_ref,
        "size_bytes": len(new_bytes),
        "validation": report.to_dict(),
        "summary": (
            f"已修改文本文件：{filename}（修改 {diff.modified}｜新增 {diff.added}｜"
            f"删除 {diff.removed}｜数字变化 {diff.numeric_changes}）"
        ),
        "result_ref": artifact_ref,
    }


async def sheet_inspect(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Read a real XLSX workbook's structure (openpyxl). Never invents facts.

    A missing / non-workbook path, an unreadable file, an unparseable workbook or
    a missing optional extra (``openpyxl``) returns ``not_executed`` (the executor
    records ``blocked``) with the verbatim reason — this handler never reports a
    structure it did not measure.
    """
    from forgeflow.documents import SheetInspectionError, inspect_sheet

    target = _sheet_target(args)
    if not target:
        return {
            "ok": False,
            "not_executed": True,
            "provider": "openpyxl",
            "reason": "未提供可读取的 .xlsx/.xlsm 文件路径（sheet_paths/paths 缺失或非工作簿），未执行",
        }
    try:
        with open(target, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        return {
            "ok": False,
            "not_executed": True,
            "provider": "openpyxl",
            "file": target,
            "reason": f"工作簿不可读取：{exc}",
        }
    try:
        structure = inspect_sheet(data, sheet=(_text(args, "sheet") or None))
    except SheetInspectionError as exc:
        return {
            "ok": False,
            "not_executed": True,
            "provider": "openpyxl",
            "file": target,
            "reason": f"工作簿解析失败：{exc}",
        }
    return {
        "ok": True,
        "provider": "openpyxl",
        "file": target,
        "format": "xlsx",
        "structure": structure.to_dict(limit=60),
        "summary": (
            f"inspect：{len(structure.sheets)} 工作表，{structure.rows} 行，"
            f"{structure.columns} 列，{structure.cells} 单元格"
        ),
        "result_ref": f"xlsx-inspect:{os.path.basename(target)}:{len(data)}",
    }


async def sheet_edit(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """The XLSX Tool layer: apply an edit intent and **really write** workbook bytes.

    Resolution order mirrors ``document.edit``: explicit ``args["edits"]`` first,
    else the LLM layer (``documents.resolve_sheet_intent``). On success the new
    bytes are persisted to the ``DocArtifactStore`` (``.xlsx``) with the payload
    carrying only the small summary + ``artifact_ref``. Untouched cells keep their
    formulas and styles; a failed verification is retried (bounded) and then
    reported honestly.
    """
    from forgeflow.documents import (
        DocArtifactStore,
        SheetEditOp,
        SheetInspectionError,
        UnknownEditOpError,
        apply_sheet_edits,
        compute_sheet_diff,
        inspect_sheet,
        resolve_sheet_intent,
        verify_sheet,
    )

    target = _sheet_target(args)
    if not target:
        return {
            "ok": False,
            "not_executed": True,
            "provider": "openpyxl",
            "reason": "未提供可读取的 .xlsx/.xlsm 文件路径（sheet_paths/paths 缺失或非工作簿），未执行（不猜测工作簿）",
        }
    try:
        with open(target, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        return {
            "ok": False,
            "not_executed": True,
            "provider": "openpyxl",
            "file": target,
            "reason": f"工作簿不可读取：{exc}",
        }
    try:
        structure = inspect_sheet(data)
    except SheetInspectionError as exc:
        return {
            "ok": False,
            "not_executed": True,
            "provider": "openpyxl",
            "file": target,
            "reason": f"工作簿解析失败：{exc}",
        }

    ops, from_intent, early = await _resolve_edit_ops(
        args,
        data,
        structure,
        op_from_dict=SheetEditOp.from_dict,
        unknown_error=UnknownEditOpError,
        resolve_intent=resolve_sheet_intent,
        provider="openpyxl",
        target=target,
    )
    if early is not None:
        return early
    assert ops is not None

    constraints = _sheet_edit_constraints(args, structure, data, ops)
    new_bytes, changes = apply_sheet_edits(data, ops)
    report = verify_sheet(new_bytes, constraints)
    max_attempts = 3 if from_intent else 1
    attempts = 0
    while not _report_passed(report) and attempts < max_attempts - 1:
        attempts += 1
        retry_ops = await resolve_sheet_intent(data, _text(args, "intent", "text"), structure)
        if not retry_ops:
            break
        new_bytes, changes = apply_sheet_edits(data, retry_ops)
        report = verify_sheet(new_bytes, constraints)
    if not _report_passed(report):
        return {
            "ok": False,
            "provider": "openpyxl",
            "file": target,
            "modified": 0,
            "added": 0,
            "removed": 0,
            "numeric_changes": 0,
            "validation": report.to_dict(),
            "reason": (
                f"校验未通过（重试 ≤{max_attempts - 1} 次后仍未满足约束）："
                f"{report.notes or '未知原因'}，未产出工作簿"
            ),
        }

    diff = compute_sheet_diff(data, new_bytes)
    try:
        artifact_ref = DocArtifactStore().put(new_bytes, ".xlsx")
    except Exception as exc:  # noqa: BLE001 — a store failure must be reported, not hidden
        return {
            "ok": False,
            "provider": "openpyxl",
            "file": target,
            "validation": report.to_dict(),
            "reason": f"文件产物落盘失败：{exc}",
        }

    filename = _sheet_filename(args, target)
    return {
        "ok": True,
        "provider": "openpyxl",
        "file": target,
        "format": "xlsx",
        "filename": filename,
        "modified": diff.modified,
        "added": diff.added,
        "removed": diff.removed,
        "numeric_changes": diff.numeric_changes,
        "changes": changes,
        "artifact_ref": artifact_ref,
        "size_bytes": len(new_bytes),
        "validation": report.to_dict(),
        "summary": (
            f"已修改工作簿：{filename}（修改 {diff.modified}｜新增 {diff.added}｜"
            f"删除 {diff.removed}｜数字变化 {diff.numeric_changes}）"
        ),
        "result_ref": artifact_ref,
    }


async def pdf_inspect(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Read a real PDF's facts (page count / chars / metadata). Never invents facts.

    Reuses ``multimodal.pdf.extract_pdf_text`` (the single truth). A missing /
    non-PDF path, an unreadable file, an unparseable PDF or a missing optional
    extra (``pypdf``) returns ``not_executed`` (the executor records ``blocked``)
    with the verbatim reason.
    """
    from forgeflow.documents import PdfInspectionError, inspect_pdf

    target = _pdf_target(args)
    if not target:
        return {
            "ok": False,
            "not_executed": True,
            "provider": "pypdf",
            "reason": "未提供可读取的 .pdf 文件路径（pdf_paths/paths 缺失或非 PDF），未执行",
        }
    try:
        with open(target, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        return {
            "ok": False,
            "not_executed": True,
            "provider": "pypdf",
            "file": target,
            "reason": f"PDF 不可读取：{exc}",
        }
    try:
        facts = inspect_pdf(data)
    except PdfInspectionError as exc:
        return {
            "ok": False,
            "not_executed": True,
            "provider": "pypdf",
            "file": target,
            "reason": f"PDF 解析失败：{exc}",
        }
    return {
        "ok": True,
        "provider": "pypdf",
        "file": target,
        "format": "pdf",
        "facts": facts.to_dict(),
        "summary": f"inspect：{facts.page_count} 页，{facts.chars} 字",
        "result_ref": f"pdf-inspect:{os.path.basename(target)}:{len(data)}",
    }


async def pdf_generate(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """The PDF Tool layer: generate a **new** PDF and persist it (fpdf2).

    Resolution order for the content spec:

      1. ``args["spec"]`` — an explicit spec (mapping or :class:`PdfGenerateSpec`);
      2. otherwise ``documents.resolve_pdf_spec`` — the LLM layer turns the intent
         text into a spec (never writes bytes itself);
      3. neither ⇒ an honest failure (``unavailable``), never a fabricated PDF.

    Does **not** read any source PDF (D6). A missing optional extra (``fpdf2``)
    returns ``not_executed`` with the verbatim reason — it never fabricates an
    empty PDF.
    """
    from forgeflow.documents import (
        DocArtifactStore,
        PdfGenerateSpec,
        PdfGenerationError,
        apply_pdf_spec,
        resolve_pdf_spec,
        verify_pdf,
    )

    spec: PdfGenerateSpec | None = None
    raw_spec = args.get("spec")
    if isinstance(raw_spec, PdfGenerateSpec):
        spec = raw_spec
    elif isinstance(raw_spec, dict):
        try:
            spec = PdfGenerateSpec.from_dict(raw_spec)
        except PdfGenerationError as exc:
            return {
                "ok": False,
                "provider": "fpdf2",
                "error": str(exc),
                "reason": f"PDF 规格非法：{exc}",
            }
    if spec is None:
        spec = await resolve_pdf_spec(_text(args, "intent", "text"))
        if not spec:
            return {
                "ok": False,
                "unavailable": True,
                "provider": "fpdf2",
                "reason": "缺少 PDF 规格(spec) 且未连接模型服务，未生成（不伪造成功）",
            }

    try:
        new_bytes = apply_pdf_spec(spec)
    except PdfGenerationError as exc:
        return {
            "ok": False,
            "not_executed": True,
            "provider": "fpdf2",
            "reason": f"PDF 生成不可用：{exc}",
        }

    constraints = _pdf_generate_constraints(args)
    report = verify_pdf(new_bytes, constraints)
    if not _report_passed(report):
        return {
            "ok": False,
            "provider": "fpdf2",
            "validation": report.to_dict(),
            "reason": f"校验未通过：{report.notes or '未知原因'}，未产出 PDF",
        }

    target = _pdf_target(args)
    try:
        artifact_ref = DocArtifactStore().put(new_bytes, ".pdf")
    except Exception as exc:  # noqa: BLE001 — a store failure must be reported, not hidden
        return {
            "ok": False,
            "provider": "fpdf2",
            "validation": report.to_dict(),
            "reason": f"文件产物落盘失败：{exc}",
        }

    filename = _pdf_filename(args, target)
    return {
        "ok": True,
        "provider": "fpdf2",
        "file": target,
        "format": "pdf",
        "filename": filename,
        "size_bytes": len(new_bytes),
        "artifact_ref": artifact_ref,
        "validation": report.to_dict(),
        "summary": f"已生成 PDF：{filename}",
        "result_ref": artifact_ref,
    }


def _kind_for_payload(payload: dict[str, Any], default: str = "document_docx") -> str:
    """The deliverable ``kind`` a document/text/sheet/pdf edit payload implies."""
    declared = str(payload.get("kind") or "").strip()
    if declared:
        return declared
    fmt = str(payload.get("format") or "").strip().lower()
    return {
        "docx": "document_docx",
        "pptx": "document_pptx",
        "text": "text_file",
        "xlsx": "spreadsheet_xlsx",
        "pdf": "pdf_document",
    }.get(fmt, default)


async def artifact_save(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Register this run's produced document / text deliverable (design §4 step ⑤).

    The source of truth is the run's **own invocation trail**, which the platform
    already threads to every tool as ``args["observations"]`` (the L2 records
    accumulated so far — see ``orchestrator._execution_args``). That is more
    honest than a caller-supplied ref: it is the run's real evidence. A direct
    ``args["artifact_ref"]`` is accepted as a fallback.

    The artifact *dict* itself is **not** built here — that projection lives in
    ``forgeflow.runtime.artifacts`` (single source of truth). This handler only
    confirms that a deliverable really exists to register; when none does it fails
    honestly rather than registering an empty deliverable.
    """
    ref = ""
    filename = ""
    kind = "document_docx"
    observations = args.get("observations")
    if isinstance(observations, list):
        for record in reversed(observations):
            if not isinstance(record, dict):
                continue
            if record.get("tool") not in (
                "document.edit",
                "textfile.edit",
                "sheet.edit",
                "pdf.generate",
            ):
                continue
            if record.get("status") != "ok":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            candidate = payload.get("artifact_ref")
            if isinstance(candidate, str) and candidate.strip():
                ref = candidate.strip()
                filename = str(payload.get("filename") or "")
                kind = _kind_for_payload(payload)
                break
    if not ref:
        direct = args.get("artifact_ref")
        if isinstance(direct, str) and direct.strip():
            ref = direct.strip()
            filename = str(args.get("filename") or "")
            kind = _kind_for_payload(dict(args))
    if not ref:
        return {
            "ok": False,
            "not_executed": True,
            "provider": "stdlib",
            "reason": "未找到可登记的文档/文本产物（artifact_ref 缺失），未登记",
        }
    return {
        "ok": True,
        "provider": "stdlib",
        "saved": True,
        "kind": kind,
        "artifact_ref": ref,
        "filename": filename or os.path.basename(ref),
        "summary": f"已登记产物：{filename or ref}",
        "result_ref": ref,
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
    "analysis.profile": analysis_profile,
    "docs.parse": docs_parse,
    "report.render": report_render,
    # INC25 W2 — the code-execution plane. ``code.execute`` drives the isolated
    # workspace + subprocess engine; ``code.commit`` is the human-in-the-loop gate.
    "code.execute": code_execute,
    "code.commit": code_commit,
    # INC43 S4 — the DOCX document-editing plane.
    "document.inspect": document_inspect,
    "document.edit": document_edit,
    "artifact.save": artifact_save,
    # INC44 §1.3 — the text / code editing plane.
    "textfile.inspect": textfile_inspect,
    "textfile.edit": textfile_edit,
    # INC45 §1.1/§1.2 — the XLSX edit plane + the PDF read/generate plane.
    "sheet.inspect": sheet_inspect,
    "sheet.edit": sheet_edit,
    "pdf.inspect": pdf_inspect,
    "pdf.generate": pdf_generate,
}
