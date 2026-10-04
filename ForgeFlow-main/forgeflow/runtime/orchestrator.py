"""Agent Runtime orchestrator — jump ① of the closed loop (docs §2).

``run_task`` drives a task to a terminal state, streaming run events on the
``RunEventBus``, validating the result, replanning on failure (up to
``MAX_REPLAN_ATTEMPTS``), escalating to HITL when the ceiling is hit, and
finally extracting an ``Experience``.

The default executor is a deterministic, dependency-free "platform graph" so
the whole loop is runnable with no LangGraph / LLM. A real compiled graph can
be injected via ``graph`` (anything exposing ``ainvoke``), which keeps this
module independent of ``forgeflow/graph/builder.py`` (which is left untouched).

INC4 §A — the Agent really uses the configured LLM. When no graph is injected
the executor is chosen by :func:`resolve_agent_runtime_mode` (``Settings
.agent_runtime_mode``: ``auto`` | ``llm`` | ``react`` | ``deterministic``).
INC17 — ``auto`` + a real provider now resolves to ``react``, the Qwen-driven
multi-round tool-calling closed loop (``runtime/react_executor``): the model
sees every tool result and keeps deciding until it stops calling tools. ``llm``
keeps :func:`_llm_executor` reachable (the pre-INC17 one-shot plan/reflect path)
as a fallback; ``deterministic`` keeps :func:`_default_executor` exactly as it
was, so the offline profile is unchanged. All paths apply the same RBAC + HITL
gates and all report real token usage through the
``TaskCreate.context["llm_usage"]`` contract.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from forgeflow.config import get_settings
from forgeflow.experience.extractor import ExperienceExtractor
from forgeflow.repositories import get_experience_repository, get_policy_repository
from forgeflow.repositories.base import new_id
from forgeflow.runtime import planning as _planning
from forgeflow.runtime.artifacts import artifacts_from_invocations
from forgeflow.runtime.events import RunEventBus, get_event_bus
from forgeflow.runtime.tool_executor import ToolCallContext, ToolExecutor
from forgeflow.validation.loop_breaker import LoopBreaker
from forgeflow.validation.replan import decide_replan, record_replan_event
from forgeflow.validation.validator import validate

logger = logging.getLogger(__name__)

# Default multi-agent **candidate** set (Supervisor → Research → Data → Code).
#
# INC15 — this is no longer a fixed plan the runtime walks unconditionally: it is
# the **candidate** set :func:`forgeflow.runtime.planning.build_plan` filters by
# applicability. A plain task (no table / paths) therefore plans only
# ``research.search`` + ``report.render``; ``data.query`` / ``code.run`` become
# ``not_applicable`` (trimmed) instead of being force-run and recorded as
# something they were not. The constant name is kept because the gate / whitelist
# tests monkeypatch it.
_DEFAULT_STEPS: list[dict[str, Any]] = [
    {"tool": "research.search", "step_type": "agent", "note": "研究助手检索资料"},
    {"tool": "data.query", "step_type": "agent", "note": "数据分析师查询数据集"},
    {"tool": "code.run", "step_type": "agent", "note": "代码开发师执行并验证"},
    # INC14/INC15 — the last step renders THIS run's plan + records into the
    # run's deliverable. Deterministic, stdlib-only, and it inherits the coarse
    # execute:workflows grant (report.render ∈ PLATFORM_TOOL_CATALOGUE), so an
    # offline run really produces an artifact instead of only a status.
    {"tool": "report.render", "step_type": "agent", "note": "生成运行产出报告"},
]

#: ``task.context`` keys that are real, caller-supplied inputs. Only these may
#: populate ``table`` / ``paths`` / ``repo_path`` — the planner never invents one.
_EXPLICIT_INPUT_KEYS: tuple[str, ...] = (
    "text",
    "query",
    "documents",
    "table",
    "paths",
    "repo_path",
    "filters",
    "limit",
    "max_results",
    # INC25 W1 / U1 — the **declared resource ids** (``context["resources"]``).
    # Additive: this is a real, caller-supplied input, and declaring it is the
    # task-body form that keeps AC-10 true — ``declared_inputs`` becomes exactly
    # ``{"resources": [id, ...]}`` (no extra key). Its value is only ever
    # **dereferenced** into the planner's ``table`` / ``paths`` / ``repo_path``
    # (see ``_capability_context``); it is never written back to ``task.context``.
    "resources",
    # INC32 ADR-03 — the Follow-up parent run id. It is a **real, caller-supplied
    # input** ("continue from run X"), so it belongs in ``declared_inputs``
    # (keeping the invariant "declared_inputs == exactly the explicit inputs the
    # planner saw"). Its value is only ever **dereferenced** into the planner's
    # ``prior_context`` (see :func:`_resolve_continued_context`); the dereferenced
    # body is **never** written back to ``task.context`` — the same "dereference,
    # never回写" contract the ``resources`` key already follows.
    "continued_from_run_id",
)

#: INC25 W2 — the code-execution plane plan tools.
_CODE_TOOLS: tuple[str, ...] = ("code.execute", "code.commit")

#: INC28 W5 — the code-plane argument keys the **platform** owns for the code
#: tools. The react executor merges them as platform-owned inputs: a value the
#: model invented for any of these (a granted ``approval`` it never received, a
#: ``workspace_id`` it never had, a fabricated ``skill_context`` …) is
#: structurally unknowable and therefore never trusted — the platform value wins
#: and a missing platform value stays absent (the tool then fails closed).
_CODEPLANE_ARG_KEYS: tuple[str, ...] = (
    "approval",
    "workspace_id",
    "prior",
    "resource_ids",
    "skill_context",
    "memory_context",
)

#: INC26 Q5 — the analysis plan tools. ``analysis.profile`` is the real
#: deterministic data-profiling step a :func:`_is_analysis_task` task gets.
_ANALYSIS_TOOLS: tuple[str, ...] = ("analysis.profile",)

#: INC43 S4 — the DOCX document-editing plane plan tools, in plan order
#: (``document.inspect`` → ``document.edit`` → ``artifact.save``; see
#: ``planning.TOOL_ORDER``). INC44 §1.2 — the plane now spans DOCX + PPTX (the
#: format is sniffed from the bytes).
_DOCUMENT_TOOLS: tuple[str, ...] = ("document.inspect", "document.edit", "artifact.save")

#: INC44 §1.3 — the text / code editing plane plan tools, in plan order
#: (``textfile.inspect`` → ``textfile.edit``; ``artifact.save`` is shared with
#: the document plane). See ``planning.TOOL_ORDER``.
_TEXTFILE_TOOLS: tuple[str, ...] = ("textfile.inspect", "textfile.edit")

#: INC45 §1.1 — the XLSX editing plane plan tools, in plan order
#: (``sheet.inspect`` → ``sheet.edit``; ``artifact.save`` is shared). See
#: ``planning.TOOL_ORDER``.
_SHEET_TOOLS: tuple[str, ...] = ("sheet.inspect", "sheet.edit")

#: INC45 §1.2 — the PDF plane plan tools. ``pdf.inspect`` is the default injected
#: step for a pdf signal; ``pdf.generate`` is injected **only** when the task
#: explicitly declares it (D1 — declaration-driven, never a fuzzy intent keyword
#: match, per ``planning``'s "no fuzzy tool selection" rule).
_PDF_TOOLS: tuple[str, ...] = ("pdf.inspect", "pdf.generate")

#: The routing priority order as predicates (D4): code > document > sheet > pdf >
#: textfile > analysis. ``_unhandled_inputs`` uses the same order.
_PLANE_PREDICATES: tuple[str, ...] = (
    "_is_code_task",
    "_is_document_task",
    "_is_sheet_task",
    "_is_pdf_task",
    "_is_textfile_task",
    "_is_analysis_task",
)


def _declared_inputs(task: TaskCreate) -> dict[str, Any]:
    """The caller's **really declared** explicit inputs (INC22 W1).

    A fresh, JSON-safe copy of exactly the ``_EXPLICIT_INPUT_KEYS`` that
    ``task.context`` genuinely carries (``is not None``). Same predicate as
    :func:`_capability_context` (``context.get(key) is not None``), so the
    persisted declaration is **exactly** what the planner saw.

    It never infers, completes or default-fills a key: a task that declared no
    ``table`` yields no ``table`` key. ``copy.deepcopy`` keeps the value verbatim
    (an int stays an int, a list stays a list) and JSON-safe, so a manual replan
    can re-declare it byte-for-byte instead of losing it or inventing one.
    """
    context = task.context or {}
    return {
        key: copy.deepcopy(context[key])
        for key in _EXPLICIT_INPUT_KEYS
        if context.get(key) is not None
    }


def _resolve_resource_inputs(context: dict[str, Any]) -> dict[str, Any]:
    """Dereference declared resource ids into planner inputs (INC25 W1 / U1).

    A thin, defensive wrapper over
    ``resources.service.ResourceService.resolve_task_inputs``: it reads the
    **registered** attributes of the resources the caller declared under
    ``context["resources"]`` and maps them onto the planner's real input keys
    (``table`` / ``paths`` / ``repo_path``). It never invents a value and returns
    ``{}`` for a task that declared nothing. Any failure degrades to ``{}`` — a
    resource lookup must never break a run. The result feeds the planner's
    ``CapabilityContext`` only; it is **never** written back to ``task.context``.
    """
    if not isinstance(context, dict) or not context.get("resources"):
        return {}
    try:
        from forgeflow.resources.service import get_resource_service

        resolved = get_resource_service().resolve_task_inputs(context)
    except Exception as exc:  # noqa: BLE001 — a resource seam must never break a run
        logger.debug("resource dereference skipped: %s", exc)
        return {}
    return dict(resolved) if isinstance(resolved, dict) else {}


def _continued_context_text(record: Any) -> str:
    """Compress a parent run into one honest ``prior_context`` paragraph.

    Reads **only** what the parent run really carries (its intent, its validator
    outcome and its artifacts' verbatim content + content fingerprint
    ``result_ref``). It never invents a conclusion: a parent with no recorded
    outcome says so, and the artifact body is a bounded excerpt of the real
    delivery — never a fabrication.
    """
    intent = str(getattr(record, "intent", "") or "").strip()
    outcome = str(getattr(record, "outcome", "") or "").strip()
    lines = [
        "上一轮任务（用户显式声明续聊）的交付要点：",
        f"- 任务：{intent or '（未记录）'}",
        f"- 结论：{outcome or '（未记录）'}",
    ]
    artifacts = [
        a
        for a in (getattr(record, "artifacts", None) or [])
        if isinstance(a, dict)
    ]
    for art in artifacts[:3]:
        title = str(art.get("title") or art.get("kind") or "交付物")
        ref = str(art.get("result_ref") or "")
        content = str(art.get("content") or "").strip()
        fingerprint = f"指纹 {ref}" if ref else "（无指纹）"
        snippet = content[:240] if content else "（无正文）"
        lines.append(f"- 交付物《{title}》[{fingerprint}]：{snippet}")
    return "\n".join(lines)


def _resolve_continued_context(context: dict[str, Any]) -> str:
    """Dereference a declared ``continued_from_run_id`` into ``prior_context``.

    Mirrors :func:`_resolve_resource_inputs`: it reads the **real** parent run
    from the in-process :class:`MemoryRunStore` and compresses it into one
    ``prior_context`` paragraph for the planner (AC-40 承重). It never fabricates
    a summary for an unknown parent and returns ``""`` when nothing was declared,
    so a plain task's planner context is byte-for-byte unchanged.

    The parent's *body* (steps / observations) is not replayed — only the header
    facts and the artifact excerpts — so the injection stays bounded and honest.
    A parent that is not in this process's store (e.g. after a restart) degrades
    to ``""`` rather than guessing.
    """
    if not isinstance(context, dict):
        return ""
    parent_id = str(context.get("continued_from_run_id") or "").strip()
    if not parent_id:
        return ""
    parent = get_run_store().get(parent_id)
    if parent is None:
        return ""
    return _continued_context_text(parent)



def _capability_context(
    task: TaskCreate,
    ctx: RequestContext,
    *,
    records: list[dict[str, Any]] | None = None,
    declared_tools: list[str] | None = None,
) -> _planning.CapabilityContext:
    """Extract the real planning signals for a run (INC15).

    The **only** legal signals are the caller's ``explicit_inputs`` (``table`` /
    ``paths`` / ``repo_path`` / ``query`` / ``text`` … from ``task.context``), the
    ``declared_tools`` (a skill / workflow / the model named them) and the
    ``workflow_type``. ``intent`` is passed through for ``query`` / ``text``
    derivation only — it is never keyword-matched against a tool.

    INC25 W1 — a declared ``resources`` list is **dereferenced** here (its
    registered ``table`` / ``paths`` / ``repo_path``) and merged into
    ``explicit_inputs`` so ``paths`` / ``repo_path``-dependent steps stop being
    blocked (AC-10). A real explicit declaration always wins over a dereferenced
    one, and the dereference never leaks back into ``task.context`` (so
    ``declared_inputs`` stays exactly what the caller wrote).
    """
    context = task.context or {}
    explicit = {
        key: context[key]
        for key in _EXPLICIT_INPUT_KEYS
        if context.get(key) is not None
    }
    for key, value in _resolve_resource_inputs(context).items():
        if explicit.get(key) is None:
            explicit[key] = value
    # INC-41 F-126 — ``analysis.profile`` resolves its target ``column`` from the
    # planner's ``explicit_inputs`` (see ``planning.resolve_inputs``), but
    # ``column`` is deliberately **not** added to ``_EXPLICIT_INPUT_KEYS`` (that
    # would change the persisted ``declared_inputs`` writeback). Surface the
    # caller's real column here instead, so the analysis contract can see it
    # without touching the declared-inputs semantics.
    if context.get("column") is not None:
        explicit["column"] = context["column"]
    # INC43 S4 — ``document.edit`` resolves its ``edits`` intent from the
    # planner's ``explicit_inputs`` (see ``planning.resolve_inputs``). Like
    # ``column`` above, ``edits`` is deliberately **not** added to
    # ``_EXPLICIT_INPUT_KEYS`` (that would change the persisted
    # ``declared_inputs`` writeback); surface the caller's real edits here
    # so the document contract can see them without touching that semantic.
    if context.get("edits") is not None:
        explicit["edits"] = context["edits"]
    if declared_tools is not None:
        declared = [str(t) for t in declared_tools]
    else:
        raw_declared = context.get("declared_tools")
        declared = (
            [str(t) for t in raw_declared]
            if isinstance(raw_declared, (list, tuple))
            else []
        )
    recs = records if records is not None else list(context.get("tool_invocations") or [])
    recs = [r for r in recs if isinstance(r, dict)]
    return _planning.CapabilityContext(
        intent=task.intent,
        workflow_type=task.workflow_type,
        explicit_inputs=explicit,
        declared_tools=declared,
        available_skills=list(getattr(ctx, "available_skills", []) or []),
        has_prior_observations=any(r.get("executed") is True for r in recs),
        observations=recs,
        # INC32 ADR-03 — the Follow-up parent-run summary, dereferenced from the
        # declared ``continued_from_run_id`` (empty for every plain task, so the
        # planner context is byte-for-byte unchanged in that case).
        prior_context=_resolve_continued_context(context),
    )


def _is_code_task(task: TaskCreate, ctx: RequestContext) -> bool:
    """Whether this task drives the code-execution plane (INC25 W2 / INC26 Q5).

    A task is a code task when it carries a code-execution signal — an approval
    resume (``codeplane_approval`` / ``codeplane_workspace_id``), an explicitly
    declared ``code.execute`` / ``code.commit`` tool, or a declared ``resources``
    entry that dereferences to a **real code source** (a non-empty
    ``repo_path``). A plain task is therefore unaffected: its plan stays
    byte-for-byte the deterministic candidate set.

    INC26 Q5 — the third signal is deliberately narrowed to ``repo_path`` only.
    The generic ``paths`` key is ALSO produced by a `FILE` resource
    (``resources/service.py::ResourceService.resolve_task_inputs`` appends a
    file's real path there), so keying on ``paths`` made **every** task that
    declared a CSV a code task and mis-routed data analysis into the code plane.
    Only a code source (``ResourceKind.GIT_REPO``) produces ``repo_path``, so this
    is the honest discriminator. ``_resolve_resource_inputs``'s output keys are
    unchanged (``table`` / ``paths`` / ``repo_path``) — the four-layer contract
    is untouched.
    """
    context = task.context or {}
    if context.get("codeplane_approval") or context.get("codeplane_workspace_id"):
        return True
    declared = context.get("declared_tools")
    if isinstance(declared, (list, tuple)) and any(str(t) in _CODE_TOOLS for t in declared):
        return True
    resolved = _resolve_resource_inputs(context)
    return bool(resolved.get("repo_path"))


def _is_document_task(task: TaskCreate, ctx: RequestContext) -> bool:
    """Whether this task drives the DOCX document-editing plane (INC43 S4).

    A task is a *document* task when it is **not** a code task and either
    explicitly declares one of :data:`_DOCUMENT_TOOLS`, or offers a real
    ``.docx`` path — from the dereferenced resources
    (:func:`_resolve_resource_inputs`) or from the caller's explicit
    ``task.context["paths"]``.

    The ordering is load-bearing: ``_is_code_task`` / ``_is_document_task``
    / ``_is_analysis_task`` are mutually exclusive (code > document >
    analysis), and :func:`_is_analysis_task` returns ``False`` for a document
    task so a ``.docx`` is never routed to the CSV profiler — the same class
    of mis-route INC26 Q5 fixed for ``repo_path``.

    The resource signal is **load-bearing** (INC43 T04-fix): a registered
    ``.docx`` FILE now dereferences to a real ``document_paths`` entry (see
    ``resources/service.ResourceService.resolve_task_inputs``), so
    ``resolved["document_paths"]`` is the reliable document signal — a
    content-addressed path carries **no** extension and would otherwise be
    invisible to the ``.docx`` suffix scan. A caller's explicit ``.docx`` path
    on ``task.context["paths"]`` and an explicitly declared document tool remain
    independent, additional signals.
    """
    if _is_code_task(task, ctx):
        return False
    context = task.context or {}
    declared = context.get("declared_tools")
    if isinstance(declared, (list, tuple)) and any(
        str(t) in _DOCUMENT_TOOLS for t in declared
    ):
        return True
    resolved = _resolve_resource_inputs(context)
    # INC43 T04-fix — a registered ``.docx`` FILE dereferences to a real
    # ``document_paths`` entry; its path is content-addressed (extensionless),
    # so it is checked BEFORE the ``.docx`` suffix scan below. This makes the
    # resource seam load-bearing, not forward-compatible.
    if resolved.get("document_paths"):
        return True
    for source in (resolved.get("paths"), context.get("paths")):
        if isinstance(source, str):
            if source.lower().endswith((".docx", ".pptx")):
                return True
        elif isinstance(source, (list, tuple)) and any(
            str(p).lower().endswith((".docx", ".pptx")) for p in source
        ):
            return True
    return False


def _is_sheet_task(task: TaskCreate, ctx: RequestContext) -> bool:
    """Whether this task drives the XLSX editing plane (INC45 §1.3).

    A task is a *sheet* task when it is **not** a code task and **not** a document
    task, and it either explicitly declares one of :data:`_SHEET_TOOLS`, or the
    resource seam dereferenced a real workbook FILE (``resolved["sheet_paths"]`` —
    a content-addressed, extensionless path a suffix scan could never see), or
    offers a real path whose suffix is ``.xlsx`` / ``.xlsm``. The signal comes
    from the extension, so it is independent of whether ``openpyxl`` is installed.

    The ordering is load-bearing: ``code > document > sheet > pdf > textfile >
    analysis`` are mutually exclusive, and :func:`_is_analysis_task` returns
    ``False`` for a sheet task so a workbook is never mis-routed to the CSV
    profiler — the defect INC45 fixes (F5/F6/F7).
    """
    if _is_code_task(task, ctx) or _is_document_task(task, ctx):
        return False
    context = task.context or {}
    declared = context.get("declared_tools")
    if isinstance(declared, (list, tuple)) and any(str(t) in _SHEET_TOOLS for t in declared):
        return True
    resolved = _resolve_resource_inputs(context)
    # A registered ``.xlsx`` FILE dereferences to a real ``sheet_paths`` entry
    # (content-addressed ⇒ extensionless) — checked BEFORE the suffix scan below.
    if resolved.get("sheet_paths"):
        return True
    for source in (resolved.get("paths"), context.get("paths")):
        if isinstance(source, str):
            if source.lower().endswith((".xlsx", ".xlsm")):
                return True
        elif isinstance(source, (list, tuple)) and any(
            str(p).lower().endswith((".xlsx", ".xlsm")) for p in source
        ):
            return True
    return False


def _is_pdf_task(task: TaskCreate, ctx: RequestContext) -> bool:
    """Whether this task drives the PDF plane (INC45 §1.3).

    A task is a *pdf* task when it is **not** a code / document / sheet task, and
    it either explicitly declares one of :data:`_PDF_TOOLS`, or the resource seam
    dereferenced a real PDF FILE (``resolved["pdf_paths"]`` — content-addressed,
    extensionless), or offers a real path whose suffix is ``.pdf``. As with
    sheets, the signal is the extension, so it is independent of whether ``pypdf``
    is installed (parsed vs metadata_only both route identically — D9).
    """
    if _is_code_task(task, ctx) or _is_document_task(task, ctx) or _is_sheet_task(task, ctx):
        return False
    context = task.context or {}
    declared = context.get("declared_tools")
    if isinstance(declared, (list, tuple)) and any(str(t) in _PDF_TOOLS for t in declared):
        return True
    resolved = _resolve_resource_inputs(context)
    if resolved.get("pdf_paths"):
        return True
    for source in (resolved.get("paths"), context.get("paths")):
        if isinstance(source, str):
            if source.lower().endswith(".pdf"):
                return True
        elif isinstance(source, (list, tuple)) and any(
            str(p).lower().endswith(".pdf") for p in source
        ):
            return True
    return False


def _pdf_generate_declared(task: TaskCreate) -> bool:
    """Whether the task explicitly declares ``pdf.generate`` (D1, declaration-driven)."""
    declared = (task.context or {}).get("declared_tools")
    return isinstance(declared, (list, tuple)) and any(
        str(t) == "pdf.generate" for t in declared
    )


def _text_extensions() -> tuple[str, ...]:
    """The registered text / code suffixes (the planner's single fact source)."""
    from forgeflow.resources.summaries import TEXT_EXTENSIONS

    return TEXT_EXTENSIONS


def _suffix_of(name: str) -> str:
    """Lower-cased final extension of a path / name (``""`` when none)."""
    text = str(name or "").strip().lower()
    dot = text.rfind(".")
    return text[dot:] if dot >= 0 else ""


def _is_textfile_task(task: TaskCreate, ctx: RequestContext) -> bool:
    """Whether this task drives the text / code editing plane (INC44 §1.4).

    A task is a *textfile* task when it is **not** a code task and **not** a
    document task, and it either explicitly declares one of
    :data:`_TEXTFILE_TOOLS`, or the resource seam dereferenced a real text / code
    FILE (``resolved["text_paths"]`` — a content-addressed, extensionless path
    that a suffix scan could never see), or offers a real path whose suffix is a
    registered text / code extension.

    The ordering is load-bearing: ``code > document > textfile > analysis`` are
    mutually exclusive, and :func:`_is_analysis_task` returns ``False`` for a
    textfile task so a code / text file is never mis-routed to the CSV profiler —
    the same class of mis-route INC26 Q5 fixed for ``repo_path`` and INC43 fixed
    for ``document_paths``. ``TABLE_EXTENSIONS`` (CSV/TSV) are NOT text
    extensions, so a CSV task still goes to the analysis plane.
    """
    if (
        _is_code_task(task, ctx)
        or _is_document_task(task, ctx)
        or _is_sheet_task(task, ctx)
        or _is_pdf_task(task, ctx)
    ):
        return False
    context = task.context or {}
    declared = context.get("declared_tools")
    if isinstance(declared, (list, tuple)) and any(
        str(t) in _TEXTFILE_TOOLS for t in declared
    ):
        return True
    resolved = _resolve_resource_inputs(context)
    # A registered text / code FILE dereferences to a real ``text_paths`` entry
    # (content-addressed ⇒ extensionless) — checked BEFORE the suffix scan below.
    if resolved.get("text_paths"):
        return True
    text_exts = set(_text_extensions())
    for source in (resolved.get("paths"), context.get("paths")):
        if isinstance(source, str):
            if _suffix_of(source) in text_exts:
                return True
        elif isinstance(source, (list, tuple)) and any(
            _suffix_of(p) in text_exts for p in source
        ):
            return True
    return False


def _is_analysis_task(task: TaskCreate, ctx: RequestContext) -> bool:
    """Whether this task should get the real analysis step (INC26 Q5).

    Mirrors :func:`_is_code_task`: a task is an *analysis* task when it is **not**
    a code task and either explicitly declares an analysis tool or declares a
    ``resources`` entry that dereferences to real data input (a non-empty
    ``paths`` for a `FILE` resource, or a ``table`` for a `DATABASE` resource).
    The two are mutually exclusive — a code task keeps the code plane and never
    gains ``analysis.profile``; a plain task with no data input gains neither.
    """
    if _is_code_task(task, ctx):
        return False
    # INC43 S4 — a document task is not an analysis task: a ``.docx`` / ``.pptx``
    # must never be profiled as a delimited data file (see _is_document_task).
    if _is_document_task(task, ctx):
        return False
    # INC44 §1.4 — a text / code file is not a data file either: excluding text
    # suffixes here is what stops a registered ``.py`` / ``.json`` from being
    # mis-routed to the CSV profiler (``TABLE_EXTENSIONS`` is unaffected, so a
    # CSV still goes to analysis).
    if _is_textfile_task(task, ctx):
        return False
    # INC45 §1.3 — a workbook / PDF is not a delimited data file either: excluding
    # the sheet / pdf planes here is what stops a registered ``.xlsx`` / ``.pdf``
    # from being mis-routed to the CSV profiler (the defect this iteration fixes).
    # ``TABLE_EXTENSIONS`` (CSV/TSV) are unaffected, so a CSV still goes to analysis.
    if _is_sheet_task(task, ctx) or _is_pdf_task(task, ctx):
        return False
    context = task.context or {}
    declared = context.get("declared_tools")
    if isinstance(declared, (list, tuple)) and any(str(t) in _ANALYSIS_TOOLS for t in declared):
        return True
    resolved = _resolve_resource_inputs(context)
    return bool(resolved.get("paths") or resolved.get("table"))


def _simulate_failure(task: TaskCreate, ctx: RequestContext) -> bool:
    """Whether an executor should append the demo *simulated* failure (INC25 P0-B).

    INC-41 F-136 — the trigger is the **explicit** ``task.context
    ["simulate_failure"]`` affordance **only**. The legacy ``"失败" in
    task.intent`` substring heuristic was **removed**: once the react path began
    honouring this helper (INC-41 QA-P2) the heuristic started force-failing
    ordinary business intents that merely contain 「失败」 (e.g.
    「分析上季度失败的订单原因」) **and** injected a fabricated failure reason
    (``模拟失败：下游工具返回异常``) into the L2/L3 execution records — both a
    functional-correctness and a **data-honesty** defect. The demo affordance is
    a test / demo hook, so it must be requested, never inferred from a word.

    ``ctx`` is retained in the signature for caller stability; it is not
    consulted (there is nothing left to scope out of the code plane).
    """
    return bool(task.context.get("simulate_failure"))


def _input_plane(path: str) -> int:
    """Routing-priority index of a declared file path by its suffix (``-1`` unknown).

    The index mirrors the mutual-exclusion order (D4): ``0`` code, ``1`` document,
    ``2`` sheet, ``3`` pdf, ``4`` textfile, ``5`` analysis. Used by
    :func:`_unhandled_inputs` to name the inputs that belong to a plane **lower**
    than the winning one.
    """
    from forgeflow.resources.summaries import TABLE_EXTENSIONS

    suffix = _suffix_of(path)
    if suffix in (".docx", ".pptx"):
        return 1
    if suffix in (".xlsx", ".xlsm"):
        return 2
    if suffix == ".pdf":
        return 3
    if suffix in set(_text_extensions()):
        return 4
    if suffix in set(TABLE_EXTENSIONS):
        return 5
    return -1


def _unhandled_inputs(task: TaskCreate, ctx: RequestContext) -> list[str]:
    """Names of explicitly declared inputs on a plane LOWER than the winner (D8).

    Execution-plane priority is unique: only the highest-priority plane runs, and
    the other declared inputs must be **stated** as unhandled rather than silently
    dropped. This pure function returns the real file **names** (basenames) of the
    caller's explicit ``task.context["paths"]`` whose routing plane sits below the
    winning plane. A registered resource's content-addressed path carries no
    extension (so it cannot be classified) and is therefore not named here — never
    a fabrication. Returns ``[]`` when nothing is unhandled.
    """
    predicates = (
        _is_code_task,
        _is_document_task,
        _is_sheet_task,
        _is_pdf_task,
        _is_textfile_task,
        _is_analysis_task,
    )
    winner = -1
    for index, predicate in enumerate(predicates):
        if predicate(task, ctx):
            winner = index
            break
    if winner < 0:
        return []
    raw = (task.context or {}).get("paths")
    if isinstance(raw, str):
        candidates = [raw]
    elif isinstance(raw, (list, tuple)):
        candidates = [str(p) for p in raw]
    else:
        candidates = []
    names: list[str] = []
    for path in candidates:
        text = str(path or "").strip()
        if not text:
            continue
        if _input_plane(text) > winner:
            base = os.path.basename(text)
            if base and base not in names:
                names.append(base)
    return names


def _unhandled_note(unhandled: list[str]) -> str:
    """The verbatim note suffix stating the unhandled inputs (D8)."""
    if not unhandled:
        return ""
    listing = "、".join(unhandled)
    return f"；另有未处理输入：{listing}（未执行，遵循执行面优先级唯一）"


def _annotate_unhandled(
    steps: list[dict[str, Any]], index: int, unhandled: list[str]
) -> None:
    """Append the unhandled-inputs note to the injecting step's ``note`` (in place)."""
    extra = _unhandled_note(unhandled)
    if extra and 0 <= index < len(steps):
        steps[index]["note"] = f"{steps[index].get('note', '')}{extra}"


def _skill_candidates(ctx: RequestContext) -> list[dict[str, Any]]:
    """Plan candidates derived from the run's injected skills (INC46 T03).

    Only a skill whose resolved version declares the **new** ``spec["procedure"]``
    key contributes anything; a skill that carries only the legacy
    ``spec["steps"]`` (``list[str]``, text) produces **no** candidates, so the
    default/plane candidate set is byte-for-byte unchanged for every run that has
    no procedure (the routing / planning nails stay green). The legacy ``steps``
    text is still injected into ``ctx.injected_skills`` as before — this function
    never touches it.

    Tool selection is taken **verbatim** from each step's declared ``tool`` and is
    filtered by the platform whitelist (``gate.PLATFORM_PLAN_TOOLS``) inside
    :func:`forgeflow.skills.runtime.to_plan_candidates` — there is no intent
    keyword match and no fuzzy substitution (the platform's "No fuzzy tool
    selection" contract).

    The imports are function-local so ``orchestrator`` and ``skills.runtime``
    never form a top-level import cycle (the same discipline
    :func:`_resolve_injected_skills` already follows).
    """
    skills = getattr(ctx, "injected_skills", None)
    if not isinstance(skills, list) or not skills:
        return []
    from forgeflow.skills.runtime import load_procedure, to_plan_candidates

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for skill in skills:
        if not isinstance(skill, dict):
            continue
        procedure = load_procedure(skill)
        if not procedure:
            continue
        for candidate in to_plan_candidates(skill, procedure):
            tool = str(candidate.get("tool") or "")
            if not tool or tool in seen:
                continue
            seen.add(tool)
            out.append(candidate)
    return out


def _skill_candidate_tools(ctx: RequestContext) -> list[str]:
    """The tool ids a run's skills declared via their ``procedure`` (INC46 T03).

    Used by :func:`_declared_plan_tools` so a skill-declared step is judged as
    *declared*: when its real input is still missing it stays visible as an honest
    ``blocked`` step instead of being trimmed as ``not_applicable``.
    """
    return [str(c.get("tool")) for c in _skill_candidates(ctx) if c.get("tool")]


# --------------------------------------------------------------------------- #
# INC46 T12 — the optional LangGraph Skill Subgraph (feature-flagged, OFF).    #
#                                                                              #
# These helpers are the *seam* the subgraph needs to become a real execution   #
# path. They are only ever reached when ``skill_subgraph_enabled()`` is True   #
# (a pure ``os.environ`` read, default OFF) — see the guarded branch in        #
# :func:`run_task`. With the flag off the branch is skipped and the executor   #
# runs byte-for-byte as before (零回归 / 红线 7).                              #
# --------------------------------------------------------------------------- #
#: The reserved default upper bound on a skill subgraph's execution budget (红线
#: 18). ``0`` means "derive the bound from the declared step count" (the T12
#: default). Exposed as a module constant **and** a parameter of
#: :func:`_drive_selected_skill_subgraph` so T28's per-run budget can override it
#: without editing this module (T12 gives the default bound, T28 supplies the
#: real one).
DEFAULT_SKILL_SUBGRAPH_MAX_ITERATIONS = 0


def _skill_subgraph_step_runner(
    task: TaskCreate, ctx: RequestContext, run_id: str, *, attempt: int = 0
):
    """Build the runner a skill-subgraph node uses to drive **one declared tool**.

    The runner routes through the platform's single honest execution entry
    (``ToolExecutor.execute`` — the same choke point the inline loop uses), so a
    subgraph-driven skill step is governed by the *same* trace / status contract:
    ``status="ok"`` only when a real handler ran and returned a result, otherwise
    an honest ``error`` / ``unavailable`` / ``blocked`` / ``refused``. The HITL /
    RBAC / whitelist gate has already run inside the subgraph's gate node
    (``skill_subgraph._decide_step``) **before** this runner is called, so a
    denied step never reaches it (红线 3 / 红线 21).

    It is a module-level factory (not a lambda) so a test can substitute a
    deterministic / mock runner without touching the production call path.
    """

    async def _runner(step, state):  # noqa: ANN001 — SkillStep / SkillRunState
        from forgeflow.runtime.tool_executor import ToolCallContext, ToolExecutor

        invocation = await ToolExecutor().execute(
            step.tool,
            ctx=ToolCallContext(
                run_id=run_id,
                step_id=f"{run_id}:skill:{int(getattr(state, 'cursor', 0))}",
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                role=ctx.role,
                intent=task.intent,
                attempt=attempt,
                args={},
            ),
            # The subgraph's HITL gate already classified this step as ``auto``
            # (a non-auto step pauses / is denied and never reaches the runner),
            # so the recorded verdict mirrors that allow — it is a *label*, not a
            # second gate (the executor never re-decides policy).
            policy_decision="allow",
        )
        return {
            "tool": step.tool,
            "status": invocation.status,
            "summary": invocation.summary,
            "executed": invocation.executed,
        }

    return _runner


def _skill_run_step_status(subgraph_status: Any) -> str:
    """Map a subgraph step record status onto the run-step status vocabulary."""
    value = str(subgraph_status or "").strip().lower()
    if value == "ok":
        return "ok"
    if value == "unavailable":
        return "unavailable"
    return "error"


def _strip_skill_procedures(ctx: RequestContext) -> None:
    """Remove the ``procedure`` key from ``ctx.injected_skills`` (flag-ON only).

    Called **only** on the flag-on path, after the subgraph has driven the
    declared procedures, so the executor does not *also* project them into its
    plan (a declared step must be driven exactly once). It never mutates the
    caller's list objects — it assigns a fresh list of shallow copies — and it is
    never reached with the flag off, so the off path is untouched (零回归).
    """
    skills = getattr(ctx, "injected_skills", None)
    if not isinstance(skills, list) or not skills:
        return
    stripped: list[dict[str, Any]] = []
    for skill in skills:
        if isinstance(skill, dict) and "procedure" in skill:
            copy = dict(skill)
            copy.pop("procedure", None)
            stripped.append(copy)
        else:
            stripped.append(skill)
    ctx.injected_skills = stripped


async def _drive_selected_skill_subgraph(
    task: TaskCreate,
    ctx: RequestContext,
    bus: RunEventBus,
    run_id: str,
    *,
    runner: Any | None = None,
    max_iterations: int = DEFAULT_SKILL_SUBGRAPH_MAX_ITERATIONS,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Drive the run's injected skill ``procedure``s through the subgraph.

    For every injected skill that declares a ``procedure`` (T03
    :func:`forgeflow.skills.runtime.load_procedure`) one shared
    :class:`~forgeflow.skills.skill_subgraph.SkillRunState` is built
    (``run_id`` / ``tenant_id`` / ``role`` / declared steps) and driven via
    :func:`~forgeflow.skills.skill_subgraph.execute_skill_run`. HITL pause /
    resume reuse T21's ``PendingActionStore`` (the subgraph reuses it — this
    helper never builds a second pause mechanism, 红线 21), and the execution
    budget is the ``max_iterations`` argument (红线 18; the reserved T28 knob).

    The subgraph's per-step outcomes are projected into the run's step-payload
    shape so the run detail renders a subgraph-driven skill step exactly like an
    inline one. A subgraph that degrades (``langgraph`` missing) or that ends on
    any non-``done`` terminal status reports the honest reason as an error —
    never a fabricated success (红线 4 / 红线 10).

    Args:
        runner: an explicit step runner (tests). ``None`` ⇒ the production
            runner from :func:`_skill_subgraph_step_runner`.
        max_iterations: the execution budget; ``0`` (default) derives it.

    Returns:
        ``(steps, errors)`` — the run-step payloads produced by the subgraph and
        the honest degradation / failure reasons (both empty when no injected
        skill declares a procedure).
    """
    skills = getattr(ctx, "injected_skills", None)
    if not isinstance(skills, list) or not skills:
        return [], []

    from forgeflow.skills import skill_subgraph as sg
    from forgeflow.skills.runtime import SkillStep, load_procedure

    steps_out: list[dict[str, Any]] = []
    errors: list[str] = []

    for skill in skills:
        if not isinstance(skill, dict):
            continue
        procedure = load_procedure(skill)
        if not procedure:
            continue

        state = sg.SkillRunState(
            run_id=run_id,
            tenant_id=ctx.tenant_id,
            role=ctx.role,
            steps=[
                SkillStep(
                    purpose=step.purpose,
                    tool=step.tool,
                    input_keys=list(step.input_keys),
                    output_keys=list(step.output_keys),
                    validation=step.validation,
                )
                for step in procedure
            ],
        )
        step_runner = (
            runner
            if runner is not None
            else _skill_subgraph_step_runner(task, ctx, run_id)
        )
        result = await sg.execute_skill_run(
            state,
            runner=step_runner,
            max_iterations=int(max_iterations),
        )

        name = str(skill.get("name") or skill.get("id") or "")
        for record in result.executed:
            index = int(record.get("index") or 0)
            label = f"技能「{name}」子图步骤 {index + 1}" if name else f"子图步骤 {index + 1}"
            steps_out.append(
                {
                    "tool": str(record.get("tool") or ""),
                    "step_type": "skill",
                    "note": label,
                    "index": len(steps_out),
                    "status": _skill_run_step_status(record.get("status")),
                    "step_id": f"{run_id}:skill:{len(steps_out)}",
                    "applicability": "required",
                    "blocked_reason": "",
                }
            )

        if result.degraded or result.status not in (sg.DONE, sg.RUNNING):
            # An honest degradation (missing langgraph / no runner) or a terminal
            # non-success (denied / expired / cancelled / failed / halted) must be
            # visible — never silently dropped (红线 4 / 红线 10).
            errors.append(
                result.reason or f"skill subgraph run {result.status}（{skill.get('id') or name}）"
            )

    return steps_out, errors


def _candidates_for(task: TaskCreate, ctx: RequestContext) -> list[dict[str, Any]]:
    """The plan candidates for ``task`` — the default set, plus task-specific steps.

    INC25 W2: for a :func:`_is_code_task` task the candidate list gains
    ``code.execute`` (immediately before ``code.run``) and ``code.commit``
    (immediately after ``code.execute``), so a code task plans the whole plane.
    Every other task keeps exactly the ``_DEFAULT_STEPS`` candidate set — the
    ``_DEFAULT_STEPS`` global is read here (not captured), so a test that
    monkeypatches it still steers this function.

    INC26 Q5: for an :func:`_is_analysis_task` task (and **not** a code task) the
    list gains ``analysis.profile`` — the symmetric counterpart of the code-step
    injection, inserted immediately before the deliverable step.

    INC46 T03: the skills this run injected (``ctx.injected_skills``) contribute
    their declared ``procedure`` candidates, prepended so a skill-declared tool is
    planned first and registered as *declared* (see :func:`_declared_plan_tools`).
    When no injected skill declares a ``procedure`` this is a no-op and the
    returned set is **byte-for-byte** the pre-INC46 one.
    """
    steps = [dict(s) for s in _DEFAULT_STEPS]
    skill_steps = _skill_candidates(ctx)
    if skill_steps:
        steps = [*skill_steps, *steps]
    if _is_code_task(task, ctx):
        exec_step = {
            "tool": "code.execute",
            "step_type": "agent",
            "note": "代码执行面：在隔离工作区执行代码任务",
        }
        commit_step = {
            "tool": "code.commit",
            "step_type": "agent",
            "note": "代码执行面：人工审批后提交变更",
        }
        out: list[dict[str, Any]] = []
        injected = False
        for step in steps:
            if step.get("tool") == "code.run" and not injected:
                out.append(exec_step)
                out.append(commit_step)
                injected = True
            out.append(step)
        if not injected:
            # The candidate set has no ``code.run`` (a monkeypatched set) — insert
            # the code steps just before the deliverable step so their order is
            # stable.
            insert_at = next(
                (i for i, s in enumerate(out) if s.get("tool") == _planning.REPORT_TOOL),
                len(out),
            )
            out[insert_at:insert_at] = [exec_step, commit_step]
        return out
    if _is_document_task(task, ctx):
        # INC43 S4 — a document task plans the whole DOCX plane, inserted
        # immediately before the deliverable step so the order is stable
        # (inspect → edit → artifact.save → report.render).
        document_steps: list[dict[str, Any]] = [
            {
                "tool": "document.inspect",
                "step_type": "agent",
                "note": "文档能力：读取 DOCX 真实结构（段落/标题/表格）",
            },
            {
                "tool": "document.edit",
                "step_type": "agent",
                "note": "文档能力：按编辑意图真正改写 DOCX 字节",
            },
            {
                "tool": "artifact.save",
                "step_type": "agent",
                "note": "文档能力：登记本次文档产物为交付物",
            },
        ]
        insert_at = next(
            (i for i, s in enumerate(steps) if s.get("tool") == _planning.REPORT_TOOL),
            len(steps),
        )
        steps[insert_at:insert_at] = document_steps
        _annotate_unhandled(steps, insert_at, _unhandled_inputs(task, ctx))
        return steps
    if _is_sheet_task(task, ctx):
        # INC45 §1.1 — a workbook task plans the XLSX plane, inserted immediately
        # before the deliverable step so the order is stable
        # (inspect → edit → artifact.save → report.render).
        sheet_steps: list[dict[str, Any]] = [
            {
                "tool": "sheet.inspect",
                "step_type": "agent",
                "note": "表格能力：读取 XLSX 工作簿真实结构（工作表/行列/表头）",
            },
            {
                "tool": "sheet.edit",
                "step_type": "agent",
                "note": "表格能力：按编辑意图真正改写工作簿字节（保留公式与样式）",
            },
            {
                "tool": "artifact.save",
                "step_type": "agent",
                "note": "表格能力：登记本次工作簿产物为交付物",
            },
        ]
        insert_at = next(
            (i for i, s in enumerate(steps) if s.get("tool") == _planning.REPORT_TOOL),
            len(steps),
        )
        steps[insert_at:insert_at] = sheet_steps
        _annotate_unhandled(steps, insert_at, _unhandled_inputs(task, ctx))
        return steps
    if _is_pdf_task(task, ctx):
        # INC45 §1.2 — a PDF task plans the PDF plane. ``pdf.generate`` is injected
        # only when the task explicitly declares it (D1 — declaration-driven).
        pdf_steps: list[dict[str, Any]] = [
            {
                "tool": "pdf.inspect",
                "step_type": "agent",
                "note": "PDF 能力：解析 PDF 真实页数与文本",
            },
        ]
        if _pdf_generate_declared(task):
            pdf_steps.append(
                {
                    "tool": "pdf.generate",
                    "step_type": "agent",
                    "note": "PDF 能力：按指令生成新的 PDF",
                }
            )
        pdf_steps.append(
            {
                "tool": "artifact.save",
                "step_type": "agent",
                "note": "PDF 能力：登记本次 PDF 产物为交付物",
            }
        )
        insert_at = next(
            (i for i, s in enumerate(steps) if s.get("tool") == _planning.REPORT_TOOL),
            len(steps),
        )
        steps[insert_at:insert_at] = pdf_steps
        _annotate_unhandled(steps, insert_at, _unhandled_inputs(task, ctx))
        return steps
    if _is_textfile_task(task, ctx):
        # INC44 §1.3 — a text / code task plans the text plane, inserted
        # immediately before the deliverable step so the order is stable
        # (inspect → edit → artifact.save → report.render).
        textfile_steps: list[dict[str, Any]] = [
            {
                "tool": "textfile.inspect",
                "step_type": "agent",
                "note": "文本能力：读取文本/代码文件真实结构（行/编码/换行）",
            },
            {
                "tool": "textfile.edit",
                "step_type": "agent",
                "note": "文本能力：按编辑意图真正改写字节（保持 EOL/编码/BOM）",
            },
            {
                "tool": "artifact.save",
                "step_type": "agent",
                "note": "文本能力：登记本次文本产物为交付物",
            },
        ]
        insert_at = next(
            (i for i, s in enumerate(steps) if s.get("tool") == _planning.REPORT_TOOL),
            len(steps),
        )
        steps[insert_at:insert_at] = textfile_steps
        _annotate_unhandled(steps, insert_at, _unhandled_inputs(task, ctx))
        return steps
    if _is_analysis_task(task, ctx):
        profile_step = {
            "tool": "analysis.profile",
            "step_type": "agent",
            "note": "分析能力：读取数据文件真实字节，统计行数与列聚合",
        }
        insert_at = next(
            (i for i, s in enumerate(steps) if s.get("tool") == _planning.REPORT_TOOL),
            len(steps),
        )
        steps[insert_at:insert_at] = [profile_step]
        _annotate_unhandled(steps, insert_at, _unhandled_inputs(task, ctx))
    return steps


def _platform_injected_tools(task: TaskCreate, ctx: RequestContext) -> list[str]:
    """The plan tools the platform injects for ``task`` (mirrors :func:`_candidates_for`).

    NOTE: :func:`_candidates_for` does **not** call this function — it decides the
    injected set itself (via :func:`_is_code_task` / :func:`_is_analysis_task`).
    This helper mirrors that *same* predicate so :func:`_declared_plan_tools` can
    report the injected tools, and only :func:`_declared_plan_tools` uses it.

    ``_candidates_for`` injects ``analysis.profile`` (analysis task) and the
    code-plane steps (code task) **because the task really carries the matching
    input signal**, so they count as *declared* for
    :func:`planning.applicability`: an injected step that is still
    under-specified (e.g. a declared CSV with no ``column``) must stay visible as
    an honest ``blocked`` step — never be trimmed away as ``not_applicable``.

    INC-41 F-126: without this, tightening the ``analysis.profile`` contract to
    require ``column`` would silently DROP the injected analysis step from the
    deterministic plan whenever no column was declared (the UI collects no
    ``column`` field), instead of reporting it blocked. The injected step is
    declared by construction, so it is registered as such here.
    """
    if _is_code_task(task, ctx):
        return list(_CODE_TOOLS)
    if _is_document_task(task, ctx):
        return list(_DOCUMENT_TOOLS)
    if _is_sheet_task(task, ctx):
        return [*_SHEET_TOOLS, "artifact.save"]
    if _is_pdf_task(task, ctx):
        tools = ["pdf.inspect"]
        if _pdf_generate_declared(task):
            tools.append("pdf.generate")
        tools.append("artifact.save")
        return tools
    if _is_textfile_task(task, ctx):
        return [*_TEXTFILE_TOOLS, "artifact.save"]
    if _is_analysis_task(task, ctx):
        return list(_ANALYSIS_TOOLS)
    return []


def _declared_plan_tools(task: TaskCreate, ctx: RequestContext) -> list[str]:
    """The union of the caller's declared tools and the platform-injected ones.

    ``planning.applicability`` reads ``declared_tools`` to decide ``blocked`` vs.
    ``not_applicable``; the deterministic executor passes this union so the
    injected ``analysis.profile`` step is judged as declared (kept as
    ``blocked`` when its input is missing) while every other candidate keeps the
    exact prior semantics.

    INC46 T03: a run's skills also (optionally) **declare** tools — the tools of
    their ``procedure`` steps (:func:`_skill_candidate_tools`). Those are added so
    a skill-declared step whose real input is missing stays an honest ``blocked``
    step rather than being trimmed as ``not_applicable``. With no injected
    procedure the union is byte-for-byte the pre-INC46 one.
    """
    raw = (task.context or {}).get("declared_tools")
    context_declared = [str(t) for t in raw] if isinstance(raw, (list, tuple)) else []
    merged = [
        *context_declared,
        *_platform_injected_tools(task, ctx),
        *_skill_candidate_tools(ctx),
    ]
    return list(dict.fromkeys(merged))


def _codeplane_args(
    task: TaskCreate, ctx: RequestContext | None = None
) -> dict[str, Any]:
    """The code-plane keys to hand ``code.execute`` / ``code.commit`` (INC25 W2).

    Reads only what the caller really put on ``task.context``: a granted approval
    (``codeplane_approval``), the workspace to reuse (``codeplane_workspace_id``),
    the prior run's codeplane evidence (``codeplane_prior``) and the declared
    ``resources`` ids. Nothing is invented — a plain run yields ``{}``.

    INC27 §8/§9 — when a run context is supplied, the **selected** Skill / Memory
    context travels too. ForgeFlow (control plane) owns those stores; OpenHands
    (execution plane) receives only this subset for this one task. Empty ⇒ the key
    is **omitted** (an honest "nothing was injected"), never a fabricated block.
    """
    context = task.context or {}
    out: dict[str, Any] = {}
    approval = context.get("codeplane_approval")
    if approval:
        out["approval"] = str(approval)
    workspace_id = context.get("codeplane_workspace_id")
    if workspace_id:
        out["workspace_id"] = str(workspace_id)
    prior = context.get("codeplane_prior")
    if isinstance(prior, dict):
        out["prior"] = prior
    resources = context.get("resources")
    if isinstance(resources, (list, tuple)):
        ids = [str(r) for r in resources if str(r or "").strip()]
        if ids:
            out["resource_ids"] = ids
    if ctx is not None:
        skills = getattr(ctx, "injected_skills", None)
        if isinstance(skills, list) and skills:
            out["skill_context"] = list(skills)
        memory = getattr(ctx, "injected_memory", None)
        if isinstance(memory, list) and memory:
            out["memory_context"] = list(memory)
    return out


def _build_task_plan(
    candidates: Any,
    task: TaskCreate,
    ctx: RequestContext,
    *,
    run_id: str,
    attempt: int,
    source: str,
    records: list[dict[str, Any]],
    reasoning: str = "",
    declared_tools: list[str] | None = None,
) -> Any:
    """Build a :class:`~forgeflow.runtime.planning.TaskPlan` with fresh inputs.

    The plan is a **pure function** of the candidates + the current context, so a
    caller may (re)build it at any point — for the report step it is built with
    the records accumulated so far, and for the run record it is rebuilt once at
    the end with every record, so its applicability labels are never stale.
    """
    cap = _capability_context(task, ctx, records=records, declared_tools=declared_tools)
    return _planning.build_plan(
        candidates,
        cap,
        run_id=run_id,
        attempt=attempt,
        source=source,
        reasoning=reasoning,
    )


def _execution_args(
    tool: str,
    task: TaskCreate,
    ctx: RequestContext,
    task_plan: Any,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Resolve one step's real args against the records accumulated so far.

    For ``report.render`` the plan ledger (plan + records + executed
    observations) is added so it can render the four-layer report; for the code
    plane tools (INC25 W2) the run's approval / workspace / prior-evidence keys
    are merged in; for every other tool the args are the planner's
    :func:`resolve_inputs` output (which always carries ``text`` / ``intent`` /
    ``query`` / ``observations``).
    """
    cap = _capability_context(task, ctx, records=records)
    args, _missing = _planning.resolve_inputs(tool, cap)
    if tool == _planning.REPORT_TOOL:
        args = {
            **args,
            "plan": task_plan.to_dict(records),
            "records": list(records),
            "observations": _planning.observations_from_records(records),
        }
    elif tool in _CODE_TOOLS:
        args = {**args, **_codeplane_args(task, ctx)}
    elif tool in _ANALYSIS_TOOLS:
        args = {**args, **_analysis_args(task, cap)}
    elif tool in _DOCUMENT_TOOLS:
        args = {**args, **_document_args(task, cap)}
    elif tool in _SHEET_TOOLS:
        args = {**args, **_sheet_args(task, cap)}
    elif tool in _PDF_TOOLS:
        args = {**args, **_pdf_args(task, cap)}
    elif tool in _TEXTFILE_TOOLS:
        args = {**args, **_textfile_args(task, cap)}
    return args


def _analysis_args(task: TaskCreate, cap: Any) -> dict[str, Any]:
    """The keys to hand ``analysis.profile`` (INC26 Q5).

    Reads only what the caller really supplied: the **already-dereferenced**
    data-file paths (from the capability context's ``explicit_inputs`` — the
    resource seam put a `FILE`'s real path there) and the caller's explicit
    ``column`` from ``task.context``. Nothing is invented: a task with no
    declared data file yields ``{}`` (the handler then honestly blocks).
    """
    out: dict[str, Any] = {}
    explicit = dict(getattr(cap, "explicit_inputs", {}) or {})
    raw = explicit.get("paths")
    paths: list[str] = []
    if isinstance(raw, (list, tuple)):
        paths = [str(p) for p in raw if str(p or "").strip()]
    elif isinstance(raw, str) and raw.strip():
        paths = [raw.strip()]
    if paths:
        out["paths"] = paths
    column = (task.context or {}).get("column")
    if isinstance(column, str) and column.strip():
        out["column"] = column.strip()
    return out


def _document_args(task: TaskCreate, cap: Any) -> dict[str, Any]:
    """The keys to hand the document tools (INC43 S4).

    Reads only what the caller really supplied: the ``.docx`` path(s) from the
    capability context's ``explicit_inputs`` — a registered ``.docx`` FILE
    dereferences to ``document_paths`` (the resource seam's real signal), and the
    caller's explicit ``paths`` is the fallback — plus the caller's explicit
    ``edits`` from ``task.context``. ``document_names`` (index-aligned with
    ``document_paths``) carries the registered original names so a produced
    deliverable keeps the user's real file name. Nothing is invented — a task with
    no declared document yields ``{}`` (the handler then honestly blocks), and the
    model never supplies these platform-owned inputs.
    """
    out: dict[str, Any] = {}
    explicit = dict(getattr(cap, "explicit_inputs", {}) or {})
    raw = explicit.get("paths")
    paths: list[str] = []
    if isinstance(raw, (list, tuple)):
        paths = [str(p) for p in raw if str(p or "").strip()]
    elif isinstance(raw, str) and raw.strip():
        paths = [raw.strip()]
    if paths:
        out["paths"] = paths
    # INC43 T04-fix — the resource seam's real document signal. A content-addressed
    # path is extensionless, so ``document_paths`` is what the handler must prefer.
    raw_docs = explicit.get("document_paths")
    document_paths: list[str] = []
    if isinstance(raw_docs, (list, tuple)):
        document_paths = [str(p) for p in raw_docs if str(p or "").strip()]
    elif isinstance(raw_docs, str) and raw_docs.strip():
        document_paths = [raw_docs.strip()]
    if document_paths:
        out["document_paths"] = document_paths
    # INC43 T04-fix — the index-aligned original names for ``document_paths``, so
    # the produced deliverable can carry the user's real file name (see
    # ``tool_handlers._document_filename``). Element positions are preserved (an
    # empty slot stays empty) to keep the two lists index-aligned.
    raw_names = explicit.get("document_names")
    document_names: list[str] = []
    if isinstance(raw_names, (list, tuple)):
        document_names = [str(n or "").strip() for n in raw_names]
    elif isinstance(raw_names, str) and raw_names.strip():
        document_names = [raw_names.strip()]
    if document_names:
        out["document_names"] = document_names
    edits = explicit.get("edits")
    if isinstance(edits, (list, tuple)) and edits:
        out["edits"] = list(edits)
    return out


def _textfile_args(task: TaskCreate, cap: Any) -> dict[str, Any]:
    """The keys to hand the text / code tools (INC44 §1.3).

    Mirrors :func:`_document_args`: reads only what the caller really supplied —
    the text / code path(s) from the capability context's ``explicit_inputs`` (a
    registered text / code FILE dereferences to ``text_paths``, the resource
    seam's real signal) plus the caller's explicit ``paths`` and ``edits``.
    ``text_names`` (index-aligned with ``text_paths``) carries the registered
    original names so a produced deliverable keeps the user's real file name.
    Nothing is invented — a task with no declared text file yields ``{}`` (the
    handler then honestly blocks).
    """
    out: dict[str, Any] = {}
    explicit = dict(getattr(cap, "explicit_inputs", {}) or {})
    raw = explicit.get("paths")
    paths: list[str] = []
    if isinstance(raw, (list, tuple)):
        paths = [str(p) for p in raw if str(p or "").strip()]
    elif isinstance(raw, str) and raw.strip():
        paths = [raw.strip()]
    if paths:
        out["paths"] = paths
    # The resource seam's real text signal (a content-addressed path carries no
    # extension, so ``text_paths`` is what the handler must prefer).
    raw_text = explicit.get("text_paths")
    text_paths: list[str] = []
    if isinstance(raw_text, (list, tuple)):
        text_paths = [str(p) for p in raw_text if str(p or "").strip()]
    elif isinstance(raw_text, str) and raw_text.strip():
        text_paths = [raw_text.strip()]
    if text_paths:
        out["text_paths"] = text_paths
    raw_names = explicit.get("text_names")
    text_names: list[str] = []
    if isinstance(raw_names, (list, tuple)):
        text_names = [str(n or "").strip() for n in raw_names]
    elif isinstance(raw_names, str) and raw_names.strip():
        text_names = [raw_names.strip()]
    if text_names:
        out["text_names"] = text_names
    edits = explicit.get("edits")
    if isinstance(edits, (list, tuple)) and edits:
        out["edits"] = list(edits)
    return out


def _sheet_args(task: TaskCreate, cap: Any) -> dict[str, Any]:
    """The keys to hand the sheet tools (INC45 §1.1).

    Mirrors :func:`_textfile_args`: reads only what the caller really supplied —
    the workbook path(s) from the capability context's ``explicit_inputs`` (a
    registered ``.xlsx`` FILE dereferences to ``sheet_paths``, the resource seam's
    real signal) plus the caller's explicit ``paths`` and ``edits``.
    ``sheet_names`` (index-aligned with ``sheet_paths``) carries the registered
    original names so a produced deliverable keeps the user's real file name.
    Nothing is invented — a task with no declared workbook yields ``{}`` (the
    handler then honestly blocks).
    """
    out: dict[str, Any] = {}
    explicit = dict(getattr(cap, "explicit_inputs", {}) or {})
    raw = explicit.get("paths")
    paths: list[str] = []
    if isinstance(raw, (list, tuple)):
        paths = [str(p) for p in raw if str(p or "").strip()]
    elif isinstance(raw, str) and raw.strip():
        paths = [raw.strip()]
    if paths:
        out["paths"] = paths
    raw_sheet = explicit.get("sheet_paths")
    sheet_paths: list[str] = []
    if isinstance(raw_sheet, (list, tuple)):
        sheet_paths = [str(p) for p in raw_sheet if str(p or "").strip()]
    elif isinstance(raw_sheet, str) and raw_sheet.strip():
        sheet_paths = [raw_sheet.strip()]
    if sheet_paths:
        out["sheet_paths"] = sheet_paths
    raw_names = explicit.get("sheet_names")
    sheet_names: list[str] = []
    if isinstance(raw_names, (list, tuple)):
        sheet_names = [str(n or "").strip() for n in raw_names]
    elif isinstance(raw_names, str) and raw_names.strip():
        sheet_names = [raw_names.strip()]
    if sheet_names:
        out["sheet_names"] = sheet_names
    edits = explicit.get("edits")
    if isinstance(edits, (list, tuple)) and edits:
        out["edits"] = list(edits)
    return out


def _pdf_args(task: TaskCreate, cap: Any) -> dict[str, Any]:
    """The keys to hand the PDF tools (INC45 §1.2).

    Mirrors :func:`_sheet_args`: reads only what the caller really supplied — the
    PDF path(s) from ``explicit_inputs`` (a registered ``.pdf`` FILE dereferences
    to ``pdf_paths``) plus the caller's explicit ``paths``; and a caller-supplied
    ``spec`` / ``pdf_names`` for generation. Nothing is invented.
    """
    out: dict[str, Any] = {}
    explicit = dict(getattr(cap, "explicit_inputs", {}) or {})
    raw = explicit.get("paths")
    paths: list[str] = []
    if isinstance(raw, (list, tuple)):
        paths = [str(p) for p in raw if str(p or "").strip()]
    elif isinstance(raw, str) and raw.strip():
        paths = [raw.strip()]
    if paths:
        out["paths"] = paths
    raw_pdf = explicit.get("pdf_paths")
    pdf_paths: list[str] = []
    if isinstance(raw_pdf, (list, tuple)):
        pdf_paths = [str(p) for p in raw_pdf if str(p or "").strip()]
    elif isinstance(raw_pdf, str) and raw_pdf.strip():
        pdf_paths = [raw_pdf.strip()]
    if pdf_paths:
        out["pdf_paths"] = pdf_paths
    raw_names = explicit.get("pdf_names")
    pdf_names: list[str] = []
    if isinstance(raw_names, (list, tuple)):
        pdf_names = [str(n or "").strip() for n in raw_names]
    elif isinstance(raw_names, str) and raw_names.strip():
        pdf_names = [raw_names.strip()]
    if pdf_names:
        out["pdf_names"] = pdf_names
    # INC45 §1.2 — a caller-supplied content spec for ``pdf.generate`` (D1). Like
    # ``column`` / ``edits``, ``spec`` is a caller-only value the model never owns.
    spec = (task.context or {}).get("spec")
    if isinstance(spec, dict):
        out["spec"] = dict(spec)
    return out


def _policy_label(decision: Any) -> str:
    """Map a PolicyEngine decision to the invocation's ``policy_decision`` label."""
    if decision is None:
        return "not_evaluated"
    if getattr(decision, "requires_approval", False):
        return "approval_required"
    effect = str(getattr(decision, "effect", "") or "").lower()
    if effect == "deny":
        return "deny"
    if effect == "allow":
        return "allow"
    return "not_evaluated"


def _record_invocation(
    task: TaskCreate,
    invocation: Any,
    records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Append an invocation to the run's cumulative trail; return its dict.

    The trail lives on ``task.context["tool_invocations"]`` so **every replan
    round** leaves its own evidence (not just the last round). ``run_task``
    copies it onto the ``RunRecord`` at the end. When ``records`` is supplied the
    payload is appended there too, so an executor keeps a local (this-attempt)
    view for input resolution without re-reading the cumulative trail.
    """
    payload = invocation.to_dict()
    task.context.setdefault("tool_invocations", []).append(payload)
    if records is not None:
        records.append(payload)
    return payload


@dataclass
class RequestContext:
    """Identity + tenant scope for a run (docs §10.5)."""

    tenant_id: str | None = None
    team_id: str | None = None
    user_id: str = "anonymous"
    role: str = "viewer"
    available_skills: list[str] = field(default_factory=list)
    #: INC27 — the platform context this run selected, and the **only** thing the
    #: code plane is handed (architecture note §8/§9: ForgeFlow owns Skill/Memory,
    #: OpenHands only receives the selected subset for this one task).
    #:
    #: Both are **same-source**: they are projected from the very same
    #: ``build_context`` bundle that fills ``available_skills``, so the code plane
    #: can never diverge from the analysis plane's ranking. Default ``[]`` so
    #: every existing construction site stays valid and "nothing was injected"
    #: is honest rather than a fabricated block.
    injected_skills: list[dict[str, Any]] = field(default_factory=list)
    injected_memory: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class TaskCreate:
    """A user intent submitted to the platform."""

    intent: str = ""
    title: str = ""
    workflow_type: str = "generic"
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunHandle:
    """Returned synchronously when a run is accepted/finished.

    INC32 (additive, default-safe) — ``session_id`` / ``parent_run_id`` carry
    the workspace relationships so a caller sees them without a re-fetch. They
    default to ``""`` so every pre-INC32 construction site stays valid and a
    plain run reports the honest empty parent (never a fabricated one).
    """

    run_id: str
    thread_id: str
    status: str
    detail: dict[str, Any] = field(default_factory=dict)
    session_id: str = ""
    parent_run_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunRecord:
    """Stored run detail — backs ``GET /runs/{id}``.

    ``total_tokens`` / ``total_cost_usd`` / ``cost_by_agent`` are the **cost
    ledger producer** (INC2 A1): ``run_task`` fills them from the run's real
    token usage via :class:`~forgeflow.observability.cost_tracker.CostTracker`.
    They default to ``0`` so a run that reports no usage (the deterministic
    platform graph, a mock/self-hosted deployment) is honestly cost-free rather
    than a fabricated figure.
    """

    run_id: str
    thread_id: str
    tenant_id: str | None
    agent_id: str | None
    intent: str
    status: str
    outcome: str
    steps: list[dict[str, Any]]
    errors: list[str]
    created_at: str
    completed_at: str | None = None
    experience_id: str | None = None
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    cost_by_agent: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Which executor produced the run — "llm" (Agent really used the provider)
    #: or "deterministic" (offline platform graph). Additive (INC4 §A).
    runtime_mode: str = "deterministic"
    #: Executor provenance: which models were actually built, whether the plan
    #: came from the LLM, and any degradation. Empty on the deterministic path
    #: so "no data" can never be mistaken for "it used the LLM".
    llm: dict[str, Any] = field(default_factory=dict)
    #: Agent Loop budget-breaker audit trail (review finding #10): the ceilings,
    #: how many times the breaker was consulted, and every breach it recorded.
    #: Empty on a run that never entered the replan loop. Defaults to ``{}`` so
    #: pre-INC5 records, and every other ``RunRecord`` construction site, stay
    #: valid without change.
    loop: dict[str, Any] = field(default_factory=dict)
    #: INC8 §3.3 (additive, default-safe): which planner produced the run's plan
    #: — ``"llm"`` (the model planned) / ``"fallback"`` (the deterministic plan,
    #: incl. the whole offline path) — and the skills the run selected
    #: (``ctx.available_skills``). Both default so **every** pre-existing
    #: construction site stays valid and the runtime behaviour is unchanged; they
    #: exist so the per-task ``planning_accuracy`` / ``skill_reuse_rate`` metrics
    #: (design §2 #3/#9) have a real source instead of a fabricated zero.
    plan_source: str = "fallback"
    skills_used: list[str] = field(default_factory=list)
    #: INC12 A1 (additive, default-safe): every **real** tool invocation this run
    #: produced, across replan rounds — each a ``ToolInvocation.to_dict()``. A
    #: step's ``status`` is truthful: only a handler that actually ran and
    #: returned a result is ``ok``; otherwise the honest
    #: ``error``/``unavailable``/``refused``/``skipped`` is recorded with a
    #: reason. Empty on a pre-INC12 record. NOTE (same as ``loop=``): the hub run
    #: store is process-lifetime and hub runs are **not** persisted, so this does
    #: not survive a restart.
    tool_invocations: list[dict[str, Any]] = field(default_factory=list)
    #: INC12 A5 (additive, default-safe): **who initiated this run** — the
    #: actor's ``user_id`` and ``role``, i.e. the truth source for the first of
    #: the "what counts as enterprise-grade" acceptance questions
    #: ("谁发起的？ → user_id / tenant_id / role"). Before A5, ``ctx.user_id`` /
    #: ``ctx.role`` flowed only into ``ToolCallContext`` and the audit events —
    #: they were never written onto the run record, so ``GET /runs/{id}`` could
    #: not answer who asked for the run. The defaults are exactly
    #: :class:`RequestContext`'s (``"anonymous"`` / ``"viewer"``), so every
    #: pre-A5 construction site stays valid and behaves unchanged.
    actor_user_id: str = "anonymous"
    actor_role: str = "viewer"
    #: INC14 — the run's real deliverables (任务产物), projected from this run's
    #: own successful ``report.render`` invocations by
    #: ``runtime.artifacts.artifacts_from_invocations``. Additive; the default
    #: keeps every other construction site valid and degrades honestly to ``[]``
    #: (the UI then shows its honest empty state rather than inventing a result).
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    #: INC15 (additive, default-safe): the run's **Task Plan** (L1) — the steps
    #: that were planned, which candidate steps did not apply and why, and the
    #: plan/execution counts (recomputed from the records). ``{}`` on a pre-INC15
    #: record so ``GET /runs/{id}`` degrades honestly rather than inventing one.
    plan: dict[str, Any] = field(default_factory=dict)
    #: INC15 (additive, default-safe): the run's **Observations** (L3) — the
    #: ``executed is True`` projection of ``tool_invocations``. A step that never
    #: executed (blocked / unavailable / refused) produces no observation.
    observations: list[dict[str, Any]] = field(default_factory=list)
    #: INC22 W1 (additive, default-safe): the run's **declared workflow type**
    #: (``task.workflow_type``, e.g. ``"sales_ops"``), captured verbatim so a
    #: manual replan (:func:`forgeflow.api.routers.runs.replan_run`) can
    #: re-declare it instead of silently falling back to ``"generic"`` — which
    #: dropped the run's real domain and often turned the re-run into a
    #: ``blocked`` one. ``"generic"`` is exactly the producer's (``TaskCreate``)
    #: own default, so every pre-INC22 record — and every other construction
    #: site — stays valid and behaves unchanged (no fabrication: a record built
    #: without it is honestly reported as the generic flow it was).
    workflow_type: str = "generic"
    #: INC22 W1 (additive, default-safe): the run's **explicit caller inputs** —
    #: ONLY the ``_EXPLICIT_INPUT_KEYS`` that ``task.context`` genuinely carried
    #: (``table`` / ``paths`` / ``repo_path`` / ``query`` / ``text`` …), each
    #: value kept **verbatim** (deep-copied, JSON-safe). Empty ``{}`` on a
    #: pre-INC22 record and on any run whose caller declared none. The platform
    #: NEVER infers, completes or default-fills a missing key here: a run that
    #: declared no ``table`` reports ``{}`` and its replan re-declares none (so it
    #: stays honestly ``blocked`` rather than being handed an invented table).
    declared_inputs: dict[str, Any] = field(default_factory=dict)
    #: INC25 W2 (additive, default-safe): the **code-execution plane** summary for
    #: a code task — the engine availability, any ``degraded`` value, the workspace
    #: lifecycle, the step timeline, the diff + reviewed test evidence and the
    #: approval state (design §7). It is a field **parallel to** ``llm`` (never a
    #: mutation of it): ``llm`` proves which model produced the run, ``codeplane``
    #: proves what the code plane did / could not do. ``{}`` on every non-code run
    #: and on any pre-INC25 record, so ``GET /runs/{id}`` degrades honestly rather
    #: than inventing an engine status.
    codeplane: dict[str, Any] = field(default_factory=dict)
    #: INC32 ADR-02 (additive, default-safe): the **workspace relationships**.
    #: ``session_id`` groups a run into a conversation (the first run of a session
    #: uses its own ``run_id``); ``parent_run_id`` records the Follow-up chain
    #: (AC-39). Both default to ``""`` so every pre-INC32 construction site stays
    #: valid; the persisted copy lives in ``workspace_runs`` (migration ``016``),
    #: the in-process copy rides on this record.
    session_id: str = ""
    parent_run_id: str = ""
    #: INC33 (additive, default-safe): whether this run's **execution detail**
    #: (``steps`` / ``tool_invocations`` / ``observations`` / ``plan`` / timeline)
    #: survived into the current process. ``True`` for a run the live process
    #: really drove. ``False`` for a record **hydrated at startup** from the
    #: persisted ``workspace_runs`` header (which holds only the header + artifacts
    #: — the run body is in-memory by design, migration ``016``): the UI must say
    #: 「执行明细未随本次进程保留」 rather than render a confident empty step list.
    #: Defaults to ``True`` so every pre-INC33 construction site stays valid.
    detail_retained: bool = True


class MemoryRunStore:
    """In-process run store (offline profile)."""

    def __init__(self) -> None:
        self._runs: dict[str, RunRecord] = {}

    def save(self, record: RunRecord) -> RunRecord:
        self._runs[record.run_id] = record
        return record

    def get(self, run_id: str) -> RunRecord | None:
        return self._runs.get(run_id)

    def list(self, tenant_id: str | None = None, limit: int = 20) -> list[RunRecord]:
        rows = list(self._runs.values())
        if tenant_id is not None:
            rows = [r for r in rows if r.tenant_id == tenant_id]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[:limit]

    def count(self, tenant_id: str | None = None) -> int:
        """How many runs this store holds for ``tenant_id`` (``None`` ⇒ all).

        INC-AUDIT — ``GET /runs`` used to report ``total=len(items)``, i.e. the
        **page** size (``list`` slices to ``limit``), so any tenant with more runs
        than the page size saw a wrong "total". The count is over the whole store,
        never over the page, so it stays truthful as ``limit`` varies.
        """
        if tenant_id is None:
            return len(self._runs)
        return sum(1 for r in self._runs.values() if r.tenant_id == tenant_id)

    def discard_session(self, session_id: str) -> int:
        """Remove every in-process run that belongs to ``session_id``; return the count.

        INC42 (P1) — this store is a **process-local cache**: ``GET /runs`` and
        ``GET /runs/{id}`` read it directly, and ``hydrate_run_store`` only ever
        *adds* headers. ``workspace/store.py::soft_delete_session`` flips
        ``deleted_at`` in the persisted ``workspace_runs`` table **only**, so
        without invalidating this cache a run this process created would still be
        served after its session was deleted — the deleted session then
        "resurrects" on the home page (``history.ts::mergeHistoryRuns`` lists the
        volatile ``GET /runs`` items first). The delete route therefore calls this
        right after a successful soft delete.

        A record is discarded when it belongs to the session explicitly
        (``session_id`` match) or implicitly (``run_id == session_id`` — the first
        run of a session uses its own ``run_id`` as the session id). ``pop``
        semantics: the keys are really removed. An empty ``session_id`` discards
        nothing (it would otherwise match every record whose ``session_id`` is
        blank). Purely additive — ``save`` / ``get`` / ``list`` / ``count`` keep
        their signatures and behaviour unchanged.
        """
        if not session_id:
            return 0
        doomed = [
            run_id
            for run_id, record in self._runs.items()
            if run_id == session_id
            or str(getattr(record, "session_id", "") or "") == session_id
        ]
        for run_id in doomed:
            del self._runs[run_id]
        return len(doomed)


_RUN_STORE = MemoryRunStore()


def get_run_store() -> MemoryRunStore:
    return _RUN_STORE


def reset_run_store() -> None:
    global _RUN_STORE
    _RUN_STORE = MemoryRunStore()


def build_running_record(
    *,
    run_id: str,
    thread_id: str,
    tenant_id: str | None,
    intent: str,
    workflow_type: str = "generic",
    session_id: str = "",
    parent_run_id: str = "",
    actor_user_id: str = "anonymous",
    actor_role: str = "viewer",
) -> RunRecord:
    """Build the pre-terminal ``running`` record for a dispatched run (INC32).

    ``created_at`` is stamped once here (and reused by the terminal record's
    creation time so the two agree); everything else matches the terminal
    record's defaults. Additive helper — the synchronous ``POST /tasks`` path
    never calls it.
    """
    now = datetime.now(timezone.utc).isoformat()
    return RunRecord(
        run_id=run_id,
        thread_id=thread_id,
        tenant_id=tenant_id,
        agent_id=None,
        intent=intent,
        status="running",
        outcome="",
        steps=[],
        errors=[],
        created_at=now,
        completed_at=None,
        workflow_type=workflow_type or "generic",
        session_id=session_id,
        parent_run_id=parent_run_id,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
    )


def register_running_run(**kwargs: Any) -> RunRecord:
    """Pre-register a ``running`` record so the run is addressable at once.

    Used by the async dispatcher (``RunDispatcher.dispatch``) and by
    ``run_task(register_running=True)``. It never clobbers an already-terminal
    record for the same id (which can only happen on a re-dispatch collision, a
    programming error the guard turns into a no-op rather than data loss).
    """
    record = build_running_record(**kwargs)
    store = get_run_store()
    existing = store.get(record.run_id)
    if existing is None or str(existing.status) == "running":
        store.save(record)
        return record
    return existing


def mark_run_terminal(
    run_id: str, *, status: str, outcome: str | None = None
) -> RunRecord | None:
    """Overwrite a stored run's status (used by the dispatcher's stop/error path).

    Returns the updated record, or ``None`` when the run is unknown. Additive —
    the normal path writes the full terminal record inside ``run_task``; this is
    only for the out-of-band transitions (``aborted`` / ``failed`` after a
    task cancellation or exception).
    """
    store = get_run_store()
    record = store.get(run_id)
    if record is None:
        return None
    record.status = status
    if outcome is not None:
        record.outcome = outcome
    if record.completed_at is None:
        record.completed_at = datetime.now(timezone.utc).isoformat()
    store.save(record)
    return record


async def persist_workspace_record(record: RunRecord) -> None:
    """Project a run record into ``workspace_runs`` (best-effort, INC32 ADR-02).

    Persistence of the relationship / header / artifacts facts must **never**
    break a run (the in-process record remains authoritative for
    ``GET /runs/{id}``), so any failure is logged and swallowed.
    """
    try:
        from forgeflow.workspace.models import WorkspaceRunRecord
        from forgeflow.workspace.store import get_workspace_store

        await get_workspace_store().save(WorkspaceRunRecord.from_run_record(record))
    except Exception as exc:  # noqa: BLE001 — persistence must never break a run
        logger.warning("workspace persistence skipped for %s: %s", record.run_id, exc)


# --------------------------------------------------------------------------- #
# Runtime mode — LLM vs. deterministic executor (INC4 §A)                      #
# --------------------------------------------------------------------------- #
#: Accepted ``Settings.agent_runtime_mode`` values. "auto" resolves from the
#: configured provider; the other three pin the path explicitly.
#: INC17 extends the set with "react" — the Qwen-driven multi-round tool-calling
#: closed loop (see :mod:`forgeflow.runtime.react_executor`).
_AGENT_RUNTIME_MODES: tuple[str, ...] = ("auto", "llm", "react", "deterministic")


def resolve_agent_runtime_mode() -> str:
    """The **effective** agent-runtime mode: ``"llm"`` / ``"react"`` / ``"deterministic"``.

    ``Settings.agent_runtime_mode`` is read defensively (``getattr``) so this
    keeps working on a ``Settings`` that predates the field. ``auto`` resolves
    from the *configured provider*:

      * ``auto`` + ``llm_provider not in ("", "mock")`` ⇒ ``react`` — a real
        provider is configured, so the Agent runs the **ReAct closed loop**
        (INC17: the model sees every tool result and keeps deciding until it
        stops calling tools). This is the INC17 default experience.
      * ``auto`` + ``llm_provider in ("", "mock")`` ⇒ ``deterministic`` — the
        offline profile keeps the pre-INC4 platform graph byte-for-byte, so the
        offline suite is unaffected.

    ``llm`` / ``react`` / ``deterministic`` pin the path regardless of provider
    (which is what lets a unit test exercise the LLM / ReAct path against a
    scripted fake model). ``react`` is also reachable offline via the explicit
    setting, so the loop is testable without a real daemon.

    INC-41 F-131 disclosure: ``deterministic`` selects the **non-LLM** executor —
    it is **not** a promise of being offline / network-free. Tool providers are
    unrelated to this mode: ``research.search`` still calls the configured
    external provider (Tavily when ``TAVILY_API_KEY`` is set) even under
    ``deterministic``. Disabling external calls is an environment/provider
    concern, not a runtime-mode one.
    """
    settings = get_settings()
    raw = getattr(settings, "agent_runtime_mode", "auto")
    requested = str(raw or "auto").strip().lower()
    if requested not in _AGENT_RUNTIME_MODES:
        logger.warning("unknown agent_runtime_mode=%r; treating as 'auto'", raw)
        requested = "auto"
    if requested == "auto":
        provider = str(getattr(settings, "llm_provider", "") or "").strip().lower()
        # INC17 — a real provider now drives the ReAct closed loop, not the
        # one-shot llm path; the offline profile stays deterministic.
        return "deterministic" if provider in ("", "mock") else "react"
    return requested


def _plan_tool_allowlist(role: str) -> list[str]:
    """Tools ``role`` may actually execute — the plan is constrained to these.

    The model's plan is untrusted output: it is filtered to tools the calling
    role holds a grant for (``runtime.gate.check_tool_permission``) so a
    hallucinated or out-of-scope tool is dropped before it can be gated. The
    RBAC + HITL gates still run on every surviving step — this is defence in
    depth, not a replacement for them.
    """
    from forgeflow.runtime.gate import check_tool_permission
    from forgeflow.runtime.llm_planner import known_plan_tools

    return sorted(tool for tool in known_plan_tools() if check_tool_permission(role, tool))


def _build_planner_models() -> tuple[Any | None, Any | None, list[dict[str, Any]]]:
    """Build the (strong, worker) chat models for the LLM path + their identity.

    Returns ``(strong_model, worker_model, identity)``. ``identity`` is a
    serialisable description of what ``get_model`` *actually* returned, emitted
    on the run so a silent degradation to a different provider/mock is visible
    (nothing here ever claims to be Ollama while being the mock). A build
    failure is logged and yields ``None`` so a bad configuration degrades to the
    deterministic plan rather than failing the run.

    ``swap_model`` (review finding ③) is resolved **per run** via
    :func:`forgeflow.cost.degrade.effective_model_strong`: while that degrade is
    in force the strong slot is served by the weak model. With no degradation in
    force the flags are unchanged, so the default path is byte-identical.
    """
    from forgeflow.cost.degrade import effective_model_strong
    from forgeflow.models import get_model
    from forgeflow.runtime.llm_planner import describe_model

    built: dict[str, Any] = {}
    for strong, label in ((True, "strong"), (False, "worker")):
        try:
            built[label] = get_model(strong=effective_model_strong(strong))
        except Exception as exc:  # noqa: BLE001 — a bad config must not kill the run
            logger.warning("LLM runtime: %s model build failed: %s", label, exc)

    identity: list[dict[str, Any]] = []
    for label in ("strong", "worker"):
        model = built.get(label)
        if model is not None:
            identity.append({"slot": label, **describe_model(model)})
    return built.get("strong"), built.get("worker"), identity


async def _llm_executor(
    task: TaskCreate,
    ctx: RequestContext,
    bus: RunEventBus,
    run_id: str,
    *,
    policy_engine: Any | None = None,
    attempt: int = 0,
) -> tuple[list[dict[str, Any]], list[str]]:
    """LLM-driven execution: Supervisor **plans** (1 call) → gated steps → **reflects** (1 call).

    This is the INC4 §A fix for the real defect: ``_default_executor`` never used
    the configured provider. Here the step list comes from the configured LLM's
    structured JSON output and the outcome is judged by the same model — so the
    Agent genuinely uses Ollama (or whatever provider is configured).

    Safety contract is **identical** to ``_default_executor`` (docs §6.3, QA V6):
    every step is re-checked against RBAC first and then risk-gated by the
    ``PolicyEngine`` *before* it runs; an RBAC denial or a high-risk HITL hit
    halts the run and returns immediately, so no tool side effect can occur.

    Cost guards (goal.md §K5, ~19 s/real call): planning is a **single** batched
    call for the whole plan and reflection is a **single** call — there is no
    per-step LLM call. Real usage is appended to ``task.context["llm_usage"]``
    (the existing ``_record_usage`` → ``CostTracker`` producer contract) so
    ``RunRecord.total_tokens`` / ``cost_by_agent`` and ``/cost/board`` get real
    numbers.

    Any unexpected failure degrades to ``_default_executor`` (and reports the
    degradation on ``task.context["llm_runtime"]``) rather than crashing a run.
    """
    from forgeflow.governance.policy_engine import PolicyEngine
    from forgeflow.runtime.gate import (
        check_code_permission,
        check_tool_permission,
        describe_code_denial,
        describe_denial,
    )
    from forgeflow.runtime.llm_planner import LLMPlanner

    strong, worker, models_info = _build_planner_models()
    primary = strong or worker
    alt = worker if strong is not None else None
    tool_allowlist = _plan_tool_allowlist(ctx.role)

    runtime_meta: dict[str, Any] = {
        "runtime_mode": "llm",
        "models": models_info,
        "allowed_tools": tool_allowlist,
        "plan": None,
        "reflection": None,
    }
    task.context["llm_runtime"] = runtime_meta

    if primary is None:
        # No model could be built at all ⇒ honest deterministic degradation.
        logger.warning("LLM runtime: no model available; using the deterministic executor")
        runtime_meta["degraded"] = "no_model"
        return await _default_executor(
            task, ctx, bus, run_id, policy_engine=policy_engine, attempt=attempt
        )

    planner = LLMPlanner(primary, alt_model=alt, allowed_tools=tool_allowlist)
    engine = policy_engine or PolicyEngine()
    simulate_failure = _simulate_failure(task, ctx)
    steps: list[dict[str, Any]] = []
    errors: list[str] = []
    records: list[dict[str, Any]] = []

    def _stash_usage() -> None:
        """Persist this executor's real usage into the run's cost contract."""
        if not planner.usage_log:
            return
        existing = list(task.context.get("llm_usage") or [])
        task.context["llm_usage"] = [*existing, *planner.usage_log]

    try:
        await bus.emit(
            run_id, "run.started", {"intent": task.intent, "workflow_type": task.workflow_type}
        )
        await bus.emit(run_id, "run.plan.started", {"runtime_mode": "llm", "models": models_info})

        # Loud guard against a *silent* provider degradation: an ``llm`` run whose
        # configured provider is a real one (not mock) but whose built model is
        # the mock means ``get_model``'s reachability probe failed (e.g. the
        # 0.5 s Ollama probe timed out). Surface it instead of quietly planning
        # on the deterministic stub.
        provider = str(getattr(get_settings(), "llm_provider", "") or "").strip().lower()
        if provider not in ("", "mock") and any(
            m.get("llm_type") == "mock" for m in models_info
        ):
            runtime_meta["degraded"] = "provider_degraded_to_mock"
            logger.error(
                "LLM runtime: configured provider %r degraded to the mock model — "
                "the run will fall back to the deterministic plan with 0 tokens",
                provider,
            )
            await bus.emit(
                run_id,
                "run.warning",
                {
                    "reason": "provider_degraded_to_mock",
                    "configured_provider": provider,
                    "models": models_info,
                    "message": (
                        f"provider '{provider}' 实际构建出 mock 模型（可达性探测失败），"
                        "本次运行将回落到确定性计划且 token 为 0"
                    ),
                },
            )

        # --- Planning: exactly one batched LLM call yields the whole plan. ---
        plan = await planner.plan(
            intent=task.intent,
            workflow_type=task.workflow_type,
            fallback_steps=_DEFAULT_STEPS,
            available_skills=list(ctx.available_skills),
            # INC32 ADR-03 — a declared Follow-up parent-run summary is appended
            # after the planner system prompt (empty ⇒ byte-identical prompt).
            prior_context=_resolve_continued_context(task.context or {}),
        )
        runtime_meta["plan"] = plan.to_dict()
        await bus.emit(run_id, "run.plan", plan.to_dict())

        # INC15 — normalise the model's chosen tools into the four-layer Task
        # Plan. The model's named tools are treated as **declared**: a step whose
        # real input is missing is kept as ``blocked`` (never silently dropped),
        # and ``report.render`` is forced last. The plan is a pure function of the
        # candidates + the records so far, so it is rebuilt whenever it is needed
        # (for the report step, and once more for the stored plan) — its labels
        # are therefore never stale.
        candidates = [
            {"tool": s.tool, "note": s.note, "step_type": s.step_type} for s in plan.steps
        ]
        declared_tools = [s.tool for s in plan.steps]
        task_plan = _build_task_plan(
            candidates,
            task,
            ctx,
            run_id=run_id,
            attempt=attempt,
            source=plan.source,
            reasoning=plan.reasoning,
            records=records,
            declared_tools=declared_tools,
        )

        def _args_for(tool: str) -> dict[str, Any]:
            """The real args for ``tool``, resolved against the records so far."""
            current_plan = _build_task_plan(
                candidates,
                task,
                ctx,
                run_id=run_id,
                attempt=attempt,
                source=plan.source,
                reasoning=plan.reasoning,
                records=records,
                declared_tools=declared_tools,
            )
            return _execution_args(tool, task, ctx, current_plan, records)

        for index, plan_step in enumerate(task_plan.steps):
            tool = plan_step.tool

            # RBAC re-check FIRST (B5 / INC2-21) — same order as the
            # deterministic executor: a denied tool aborts before the risk gate.
            if not check_tool_permission(ctx.role, tool):
                errors.append(describe_denial(ctx.role, tool))
                _stash_usage()
                await bus.emit(
                    run_id,
                    "run.error",
                    {"message": errors[-1], "tool": tool, "reason": "rbac_denied"},
                )
                return steps, errors

            # INC27 — the code-plane Policy Engine (architecture note §2): the
            # coarse run grant is not enough to drive the code agent. Checked
            # after the general RBAC gate and still BEFORE the risk gate, so a
            # denied code step cannot produce any side effect.
            if not check_code_permission(ctx.role, tool):
                errors.append(describe_code_denial(ctx.role, tool))
                _stash_usage()
                await bus.emit(
                    run_id,
                    "run.error",
                    {"message": errors[-1], "tool": tool, "reason": "code_rbac_denied"},
                )
                return steps, errors

            decision = await engine.evaluate_tool_call(
                ctx.user_id,
                ctx.role,
                tool,
                tenant_id=ctx.tenant_id,
                run_id=run_id,
                context={"intent": task.intent, "planned_by": plan.source},
            )
            if decision.requires_approval:
                errors.append(f"高风险工具 '{tool}' 已被 HITL 拦截，等待人工审批")
                _stash_usage()
                await bus.emit(
                    run_id,
                    "run.error",
                    {
                        "message": errors[-1],
                        "tool": tool,
                        "risk_level": decision.risk_level,
                        "approval_id": getattr(decision, "approval_id", None),
                    },
                )
                return steps, errors

            await bus.emit(
                run_id,
                "run.step",
                {"index": index, "tool": tool, "note": plan_step.note, "status": "running"},
            )
            await asyncio.sleep(0)  # yield to the event loop so SSE can flush
            # INC12 A1 — the step is executed through the single honest entry
            # point. ``run_step.done`` reports the REAL status: only a handler
            # that actually ran and returned a result is ``ok``.
            cap_now = _capability_context(task, ctx, records=records)
            blocked_reason = _planning.blocked_reason(tool, cap_now)
            invocation = await ToolExecutor().execute(
                tool,
                ctx=ToolCallContext(
                    run_id=run_id,
                    step_id=plan_step.step_id,
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    role=ctx.role,
                    intent=task.intent,
                    attempt=attempt,
                    args=_args_for(tool),
                ),
                policy_decision=_policy_label(decision),
                approval_id=getattr(decision, "approval_id", None),
                blocked_reason=blocked_reason or None,
            )
            observation = _record_invocation(task, invocation, records)
            payload = plan_step.to_payload(index, status=invocation.status)
            payload["observation"] = observation if invocation.executed else None
            steps.append(payload)
            if invocation.executed:
                await bus.emit(run_id, "run.observation", observation)
            await bus.emit(
                run_id,
                "run.step.done",
                {
                    "index": index,
                    "tool": tool,
                    "status": invocation.status,
                    "observation": payload["observation"],
                },
            )
            if invocation.status in ("error", "unavailable", "refused"):
                errors.append(
                    f"工具 '{tool}' 未成功执行：{invocation.error or invocation.summary}"
                )
            elif invocation.status == "blocked":
                # A blocked step is not a failure (missing input) but must not be
                # invisible either — record a warning, never a fake ok.
                await bus.emit(
                    run_id,
                    "run.warning",
                    {
                        "reason": "tool_blocked",
                        "tool": tool,
                        "message": f"工具 '{tool}' 需要执行但缺少必需输入，已受阻：{invocation.summary}",
                    },
                )
            elif invocation.status == "awaiting_approval":
                # INC25 W2 — the code change is gated on a human decision; halt
                # here so no report is rendered for an unapproved change.
                await bus.emit(
                    run_id,
                    "run.awaiting_approval",
                    {"tool": tool, "message": invocation.summary, "step_id": plan_step.step_id},
                )
                break

        # Rebuild the plan with every record so the stored plan is never stale.
        task.context["plan"] = _build_task_plan(
            candidates,
            task,
            ctx,
            run_id=run_id,
            attempt=attempt,
            source=plan.source,
            reasoning=plan.reasoning,
            records=records,
            declared_tools=declared_tools,
        ).to_dict(records)

        if simulate_failure:
            errors.append("模拟失败：下游工具返回异常")
            await bus.emit(run_id, "run.error", {"message": errors[-1]})

        # --- Reflection: exactly one batched call judging the executed plan. ---
        reflection = await planner.reflect(
            intent=task.intent,
            plan_signature=" -> ".join(s.tool for s in plan.steps),
            steps=steps,
            errors=errors,
        )
        runtime_meta["reflection"] = reflection.to_dict()
        await bus.emit(run_id, "run.reflection", reflection.to_dict())
        if reflection.success is False and not errors:
            errors.append(
                f"LLM 反思判定执行结果未满足意图：{reflection.summary or '（无摘要）'}"
            )
            await bus.emit(
                run_id,
                "run.error",
                {"message": errors[-1], "reason": "reflection_failed"},
            )

        _stash_usage()
        return steps, errors
    except Exception as exc:  # noqa: BLE001 — never let an LLM hiccup crash a run
        logger.warning(
            "LLM runtime failed (%s); degrading to the deterministic executor",
            exc,
            exc_info=True,
        )
        # INC37-QA: 带上异常类型 —— 裸异常（str 为空）不能留成 "exception: "。
        runtime_meta["degraded"] = f"exception: {type(exc).__name__}: {exc}"
        _stash_usage()
        return await _default_executor(
            task, ctx, bus, run_id, policy_engine=policy_engine, attempt=attempt
        )


async def _default_executor(
    task: TaskCreate,
    ctx: RequestContext,
    bus: RunEventBus,
    run_id: str,
    *,
    policy_engine: Any | None = None,
    attempt: int = 0,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Deterministic multi-agent execution driven by a **dynamic** plan (INC15).

    The step list is not a constant: :func:`planning.build_plan` turns the
    candidate set into a :class:`~forgeflow.runtime.planning.TaskPlan` for *this*
    task — irrelevant candidates are trimmed (``not_applicable``), a declared but
    under-specified step is kept as ``blocked`` with a reason. Every step whose
    real inputs are available is risk-gated by the ``PolicyEngine`` (docs §6.3,
    QA V6) before it runs; a high-risk tool call raises a HITL approval and
    **halts the run before the tool executes**.
    """
    from forgeflow.governance.policy_engine import PolicyEngine
    from forgeflow.runtime.dispatcher import is_run_cancelled
    from forgeflow.runtime.gate import (
        check_code_permission,
        check_tool_permission,
        describe_code_denial,
        describe_denial,
    )

    engine = policy_engine or PolicyEngine()
    simulate_failure = _simulate_failure(task, ctx)
    steps: list[dict[str, Any]] = []
    errors: list[str] = []
    records: list[dict[str, Any]] = []
    await bus.emit(run_id, "run.started", {"intent": task.intent, "workflow_type": task.workflow_type})

    # INC25 W2 — the candidate set for THIS task: the deterministic default, plus
    # the code-execution plane steps when the task is a code task (a plain task
    # keeps the exact default set). Read fresh from the module global so a test
    # that monkeypatches ``_DEFAULT_STEPS`` still steers the plan.
    candidates = _candidates_for(task, ctx)
    plan = _build_task_plan(
        candidates,
        task,
        ctx,
        run_id=run_id,
        attempt=attempt,
        source="deterministic",
        records=records,
        # INC-41 F-134 — the *initial* plan (the one that drives the execution
        # loop and is emitted as the SSE ``run.plan`` first frame) must use the
        # SAME declared-tools union as the final plan. Without it, a
        # platform-injected step (e.g. ``analysis.profile`` for a declared data
        # file with no ``column``) was trimmed as ``not_applicable`` while the
        # loop ran — no ``blocked`` record, no ``run.warning``, and a first frame
        # inconsistent with the terminal plan.
        declared_tools=_declared_plan_tools(task, ctx),
    )
    await bus.emit(run_id, "run.plan", plan.to_dict(records))

    for index, step in enumerate(plan.steps):
        tool = step.tool

        # INC32 ADR-04 — cooperative cancel: a Stop requested between steps stops
        # the run here instead of producing one more step/deliverable (AC-36). The
        # subsequent CancelledError is caught by the dispatcher, which emits
        # ``run.aborted`` and marks the run aborted. No-op on the sync path
        # (``is_run_cancelled`` is False when no dispatcher exists).
        if is_run_cancelled(run_id):
            raise asyncio.CancelledError()

        # B5 (INC2-21): RBAC re-check FIRST. The route gate only ever saw
        # "workflows:execute"; a role that may start a run can still be barred
        # from a specific tool. Denied ⇒ run.error and return *before* the tool
        # (and before the risk gate) is reached.
        if not check_tool_permission(ctx.role, tool):
            errors.append(describe_denial(ctx.role, tool))
            await bus.emit(
                run_id,
                "run.error",
                {"message": errors[-1], "tool": tool, "reason": "rbac_denied"},
            )
            return steps, errors

        # INC27 — code-plane Policy Engine: the coarse run grant alone must not
        # let a role drive the code agent. Still before the risk gate.
        if not check_code_permission(ctx.role, tool):
            errors.append(describe_code_denial(ctx.role, tool))
            await bus.emit(
                run_id,
                "run.error",
                {"message": errors[-1], "tool": tool, "reason": "code_rbac_denied"},
            )
            return steps, errors

        decision = await engine.evaluate_tool_call(
            ctx.user_id,
            ctx.role,
            tool,
            tenant_id=ctx.tenant_id,
            run_id=run_id,
            context={"intent": task.intent},
        )
        if decision.requires_approval:
            errors.append(f"高风险工具 '{tool}' 已被 HITL 拦截，等待人工审批")
            await bus.emit(
                run_id,
                "run.error",
                {
                    "message": errors[-1],
                    "tool": tool,
                    "risk_level": decision.risk_level,
                    "approval_id": getattr(decision, "approval_id", None),
                },
            )
            return steps, errors

        await bus.emit(
            run_id,
            "run.step",
            {"index": index, "tool": tool, "note": step.note, "status": "running"},
        )
        await asyncio.sleep(0)  # yield to the event loop so SSE can flush

        # Resolve the step's *real* inputs against the records accumulated so far
        # (observations grow as earlier steps execute), then execute through the
        # single honest entry point. ``blocked_reason`` non-empty ⇒ the handler is
        # never called and an honest ``blocked`` record is written.
        cap_now = _capability_context(task, ctx, records=records)
        blocked_reason = _planning.blocked_reason(tool, cap_now)
        invocation = await ToolExecutor().execute(
            tool,
            ctx=ToolCallContext(
                run_id=run_id,
                step_id=step.step_id,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                role=ctx.role,
                intent=task.intent,
                attempt=attempt,
                args=_execution_args(tool, task, ctx, plan, records),
            ),
            policy_decision=_policy_label(decision),
            approval_id=getattr(decision, "approval_id", None),
            blocked_reason=blocked_reason or None,
        )
        observation = _record_invocation(task, invocation, records)
        payload = step.to_payload(index, status=invocation.status)
        # INC15 — an observation exists only for a step that really executed;
        # a blocked/unavailable/refused step carries ``None`` rather than a
        # fabricated observation.
        payload["observation"] = observation if invocation.executed else None
        steps.append(payload)
        if invocation.executed:
            await bus.emit(run_id, "run.observation", observation)
        await bus.emit(
            run_id,
            "run.step.done",
            {
                "index": index,
                "tool": tool,
                "status": invocation.status,
                "observation": payload["observation"],
            },
        )
        if invocation.status in ("error", "unavailable", "refused"):
            errors.append(
                f"工具 '{tool}' 未成功执行：{invocation.error or invocation.summary}"
            )
        elif invocation.status == "blocked":
            # A blocked step is not a failure (it is missing input) but must not
            # be invisible either — record a warning, never a fake ok.
            await bus.emit(
                run_id,
                "run.warning",
                {
                    "reason": "tool_blocked",
                    "tool": tool,
                    "message": f"工具 '{tool}' 需要执行但缺少必需输入，已受阻：{invocation.summary}",
                },
            )
        elif invocation.status == "awaiting_approval":
            # INC25 W2 — the code change is gated on a human decision. The run
            # halts here (later steps would render a report for an unapproved
            # change); the orchestrator surfaces ``status="awaiting_approval"``.
            await bus.emit(
                run_id,
                "run.awaiting_approval",
                {"tool": tool, "message": invocation.summary, "step_id": step.step_id},
            )
            break

    # Rebuild the plan with every record so the stored plan's counts/labels are
    # never stale, then stash it for the RunRecord.
    final_plan = _build_task_plan(
        _candidates_for(task, ctx),
        task,
        ctx,
        run_id=run_id,
        attempt=attempt,
        source="deterministic",
        records=records,
        declared_tools=_declared_plan_tools(task, ctx),
    )
    task.context["plan"] = final_plan.to_dict(records)

    if simulate_failure:
        errors.append("模拟失败：下游工具返回异常")
        await bus.emit(run_id, "run.error", {"message": errors[-1]})
    return steps, errors


async def _execute_graph(
    graph: Any, task: TaskCreate, ctx: RequestContext, bus: RunEventBus, run_id: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """Run an injected graph (anything with ``ainvoke``) and normalise output."""
    state = {
        "messages": [],
        "workflow_id": run_id,
        "intent": task.intent,
        "lead_data": task.context,
        "errors": [],
    }
    result = await graph.ainvoke(state)
    steps = list(result.get("steps", _DEFAULT_STEPS)) if isinstance(result, dict) else list(_DEFAULT_STEPS)
    errors = list(result.get("errors", [])) if isinstance(result, dict) else []
    for index, step in enumerate(steps):
        await bus.emit(run_id, "run.step.done", {"index": index, **dict(step)})
    return steps, errors


async def _tenant_spend_usd(tenant_id: str | None) -> float:
    """The tenant's real billable spend, read from the cost ledger (INC2 A1).

    This is the missing **producer → consumer** link: the budget/degrade path
    (``_resolve_context_budget``) used to read ``task.context["cost_usd"]``,
    which nothing in the repo ever wrote — so the degrade action chain never
    fired. When the caller supplies no explicit ``cost_usd`` we now fall back to
    the tenant's accumulated spend so ``trim_context`` / ``swap_model`` can
    actually trigger. Any ledger error degrades to ``0.0`` (a lookup must never
    break a run).
    """
    try:
        from forgeflow.cost.ledger import current_spend

        return await current_spend(tenant_id)
    except Exception as exc:  # noqa: BLE001 — degrade lookup must never break a run
        logger.debug("tenant spend lookup skipped: %s", exc)
        return 0.0


def _tokens_so_far(task: TaskCreate) -> int:
    """Cumulative real token usage reported by the executor(s) so far.

    The loop breaker needs the tokens the *run* has already spent, which is
    exactly the ``TaskCreate.context["llm_usage"]`` contract the LLM executor
    appends to (and that :func:`_record_usage` later feeds to the cost ledger).
    A malformed entry is skipped rather than raising inside the run loop.
    """
    total = 0
    raw = task.context.get("llm_usage") or []
    entries = [raw] if isinstance(raw, dict) else list(raw)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            total += int(entry.get("input_tokens") or 0)
            total += int(entry.get("output_tokens") or 0)
        except (TypeError, ValueError):
            logger.debug("ignoring malformed llm_usage entry: %r", entry)
    return total


def _default_usage_model() -> str:
    """Model name to assume for a usage record that does not name one.

    Chosen from the configured provider so a mock/self-hosted deployment is
    never accidentally billed at a cloud rate (``mock``/``ollama*`` → ``0.0``).
    INC16: OpenAI / Anthropic were removed, so any non-Ollama provider falls back
    to the Ollama model tag (never an external cloud model).
    """
    settings = get_settings()
    provider = (settings.llm_provider or "").strip().lower()
    if provider == "mock":
        return "mock"
    if provider in ("", "ollama"):
        return settings.ollama_model
    # Any other (incl. the INC16-removed openai/anthropic) — never assume a
    # cloud model rate; fall back to the neutral mock label (billed at 0.0).
    return "mock"


def _record_usage(tracker: Any, raw: Any) -> None:
    """Feed a run's reported token usage into ``tracker`` (INC2 A1 producer).

    The deterministic platform graph makes no LLM calls, so on a real run this
    is empty and the run is cost-free (correct for mock/Ollama). A caller that
    *did* spend tokens reports them on ``TaskCreate.context["llm_usage"]`` as a
    list of ``{"agent", "model"?, "input_tokens", "output_tokens"}`` dicts; each
    entry is recorded with :meth:`CostTracker.record`, which applies the pricing
    policy in :mod:`forgeflow.observability.cost_tracker`.
    """
    if not raw:
        return
    entries = [raw] if isinstance(raw, dict) else list(raw)
    default_model = _default_usage_model()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            agent = str(entry.get("agent") or entry.get("agent_name") or "unknown")
            model = str(entry.get("model") or default_model)
            input_tokens = int(entry.get("input_tokens") or 0)
            output_tokens = int(entry.get("output_tokens") or 0)
        except (TypeError, ValueError):
            logger.debug("ignoring malformed llm_usage entry: %r", entry)
            continue
        tracker.record(agent, input_tokens, output_tokens, model=model)


async def _resolve_context_budget(task: TaskCreate, ctx: RequestContext) -> int:
    """Token budget for ``build_context`` — halved when A1's ``trim_context`` fires.

    A1↔A5 link (architecture §2.1 step 4): when the tenant's budget decision
    carries ``trim_context``, the context budget drops by 50% instead of running
    at full width and blowing the cost envelope.
    """
    budget = int(get_settings().context_budget_tokens)
    try:
        from forgeflow.cost.budget_service import BudgetService
        from forgeflow.cost.degrade import build_degrade_state

        explicit_spent = task.context.get("cost_usd")
        if explicit_spent is None:
            # No caller-supplied figure ⇒ use the tenant's real ledger spend so
            # the degrade chain can actually trigger (see _tenant_spend_usd).
            spent = await _tenant_spend_usd(ctx.tenant_id)
        else:
            spent = float(explicit_spent or 0.0)
        decision = await BudgetService().evaluate_budget(
            ctx.tenant_id, spent, team_id=ctx.team_id
        )
        state = build_degrade_state(decision.actions)
        budget = state.effective_context_budget(budget)
    except Exception as exc:  # noqa: BLE001 — a degrade lookup must never break a run
        logger.debug("context budget degrade lookup skipped: %s", exc)
    return budget


async def _build_run_context(
    task: TaskCreate, ctx: RequestContext, run_id: str
) -> Any:
    """INC2-10 — the single runtime call site of ``build_context`` (A5).

    Fills ``ctx.available_skills`` from the selected skill sections and records
    the compression / hit-rate stats for the ops page.
    """
    from forgeflow.experience.context_builder import build_context
    from forgeflow.observability.context_stats import (
        persist_context_build,
        record_context_build,
    )

    budget = await _resolve_context_budget(task, ctx)
    bundle = await build_context(
        ctx.tenant_id,
        task.intent,
        user_id=ctx.user_id,
        team_id=ctx.team_id,
        budget_tokens=budget,
    )
    entry = record_context_build(bundle, tenant_id=ctx.tenant_id, run_id=run_id)
    await persist_context_build(entry)
    return bundle


async def _resolve_injected_skills(
    tenant_id: str | None, skill_ids: list[str]
) -> list[dict[str, Any]]:
    """Resolve selected skill ids into ``{id, name, version, steps[, procedure]}``.

    INC27: :func:`_build_run_context` keeps only a skill's **id** on each section
    (the section text is ``"name: description"``), but the code plane must hand
    the agent the skill's *steps* and must record the *version* it really used
    (AC-2 / AC-3). Both come from the registry: ``SkillRegistry.get`` gives
    ``name`` / ``current_version``, and the version record matching that semver
    carries ``spec["steps"]``.

    Non-gating by design (context is an enhancement, not a gate): any failure logs
    and yields ``[]`` while the ids stay on ``available_skills``; a skill whose
    version cannot be resolved is **skipped**, never padded with a fabricated one.
    """
    ids = [str(s) for s in (skill_ids or []) if str(s or "").strip()]
    if not ids:
        return []
    out: list[dict[str, Any]] = []
    try:
        from forgeflow.skills.registry import SkillRegistry

        registry = SkillRegistry()
        for skill_id in ids:
            try:
                record = await registry.get(tenant_id, skill_id)
            except Exception as exc:  # noqa: BLE001 — one bad skill never gates a run
                logger.warning("code-plane skill resolve failed for %s: %s", skill_id, exc)
                continue
            if record is None:
                continue
            version = str(getattr(record, "current_version", "") or "")
            steps: list[str] = []
            procedure: list[dict[str, Any]] = []
            if version:
                try:
                    versions = await registry.versions(tenant_id, skill_id)
                except Exception:  # noqa: BLE001
                    versions = []
                match = next(
                    (v for v in versions if str(getattr(v, "semver", "")) == version), None
                )
                spec = getattr(match, "spec", None) if match is not None else None
                if isinstance(spec, dict):
                    raw_steps = spec.get("steps")
                    if isinstance(raw_steps, (list, tuple)):
                        steps = [str(s) for s in raw_steps if str(s or "").strip()]
                    # INC46 T03 — the **new** structured ``procedure`` key travels
                    # with the injected skill so the runtime can really drive it
                    # step by step. The legacy ``steps`` above is untouched.
                    raw_procedure = spec.get("procedure")
                    if isinstance(raw_procedure, (list, tuple)):
                        procedure = [
                            dict(item)
                            for item in raw_procedure
                            if isinstance(item, dict)
                        ]
            entry: dict[str, Any] = {
                "id": str(getattr(record, "id", "") or skill_id),
                "name": str(getattr(record, "name", "") or ""),
                "version": version,
                "description": str(getattr(record, "description", "") or ""),
                "steps": steps,
            }
            # Additive (INC46 T03): the ``procedure`` key is present only when the
            # skill's version really declares one, so a skill without a procedure
            # keeps the exact pre-INC46 shape — the code-plane context block and
            # the transport-parity nails are byte-for-byte unaffected.
            if procedure:
                entry["procedure"] = procedure
            out.append(entry)
    except Exception as exc:  # noqa: BLE001
        logger.warning("code-plane skill context unavailable: %s", exc)
    return out


async def _persist_loop_breaker_breadcrumb(
    ctx: RequestContext, run_id: str, breach: Any, breaker: Any
) -> None:
    """INC9 B4 / O1 — durable breadcrumb when the Agent Loop budget trips.

    The hub run record is **process-lifetime** (hub runs are not persisted — see
    the ``loop=`` comment in ``run_task``), so without this the reason a loop
    stopped would vanish on restart. This writes one entry to the **existing**
    audit sink (``middleware.audit.write_audit_entry`` — the PostgreSQL
    ``audit_log`` table, else the offline ring buffer), the same sink
    ``/audit/search`` + ``/audit/export`` read. No new component, no new table,
    no API change; best-effort (the sink swallows its own failures, so a broken
    audit can never affect the run).
    """
    from forgeflow.middleware.audit import write_audit_entry

    await write_audit_entry(
        {
            "user_id": getattr(ctx, "user_id", None),
            "role": getattr(ctx, "role", None) or "unknown",
            "action": "run.loop.breaker",
            "resource": "runs",
            "resource_id": run_id,
            "outcome": "denied",
            "workspace_id": getattr(ctx, "tenant_id", None),
            "metadata": {
                "dimension": getattr(breach, "dimension", None),
                "breach": breach.to_dict() if hasattr(breach, "to_dict") else {},
                "ceilings": (
                    breaker.budget.to_dict()
                    if getattr(breaker, "budget", None) is not None
                    and hasattr(breaker.budget, "to_dict")
                    else {}
                ),
            },
        }
    )


def _has_awaiting_approval(steps: list[dict[str, Any]] | None) -> bool:
    """Whether any recorded step is gated on a human decision (INC25 W2)."""
    for step in steps or []:
        if isinstance(step, dict) and str(step.get("status") or "") == "awaiting_approval":
            return True
    return False


def _codeplane_payload_of(invocation: dict[str, Any]) -> dict[str, Any] | None:
    """The ``codeplane`` sub-dict a code-plane invocation carried (``None``)."""
    payload = invocation.get("payload")
    if not isinstance(payload, dict):
        return None
    cp = payload.get("codeplane")
    return cp if isinstance(cp, dict) else None


def _assemble_codeplane(
    records: list[dict[str, Any]] | None,
    steps: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Assemble ``RunRecord.codeplane`` from the run's code-plane invocations.

    Reads **only** what the code-plane handlers really reported (the
    ``codeplane`` sub-dict on a ``code.execute`` / ``code.commit`` invocation
    payload). It never invents an engine status: a run that produced no such
    invocation yields ``{}``. The named ``affected_steps`` / ``completed_steps``
    come from the run's own step payloads — a step that was unimplemented / failed
    is named, a step that ran is named, and nothing is padded.
    """
    exec_cp: dict[str, Any] | None = None
    commit_cp: dict[str, Any] | None = None
    for inv in records or []:
        if not isinstance(inv, dict):
            continue
        cp = _codeplane_payload_of(inv)
        if cp is None:
            continue
        if inv.get("tool") == "code.execute":
            exec_cp = cp
        elif inv.get("tool") == "code.commit":
            commit_cp = cp
    base = commit_cp or exec_cp
    if base is None:
        return {}
    merged: dict[str, Any] = {
        "engine": dict(base.get("engine") or {}),
        "degraded": base.get("degraded"),
        "workspace": dict(base.get("workspace") or {}),
        "timeline": [e for e in (base.get("timeline") or []) if isinstance(e, dict)],
        "tests": dict(base.get("tests") or {}) if isinstance(base.get("tests"), dict) else {},
        "diff": str(base.get("diff") or ""),
        # INC29 T02 (§6) — the progress-summary vocabulary, when the handler
        # produced one (a degraded / older payload carries none ⇒ ``{}``).
        "summary": dict(base.get("summary") or {}) if isinstance(base.get("summary"), dict) else {},
    }
    # INC28 W4 — the explicit code-task outcome, read verbatim from the handler's
    # codeplane sub-dict (never re-derived here). The exec payload is authoritative
    # when present; the commit payload's value is the fallback.
    outcome_source = exec_cp if (exec_cp or {}).get("outcome") else base
    if isinstance(outcome_source, dict) and outcome_source.get("outcome"):
        merged["outcome"] = str(outcome_source["outcome"])
    # The exec evidence is the authoritative source for the engine / workspace /
    # timeline / diff / tests; the commit payload only adds the approval / commit
    # outcome on top.
    if exec_cp is not None:
        merged["degraded"] = exec_cp.get("degraded")
        if exec_cp.get("engine"):
            merged["engine"] = dict(exec_cp.get("engine") or {})
        # INC25 P0-B — the workspace is the *exec* payload's, never the commit
        # payload's. A **pending** ``code.commit`` (the original, un-approved run)
        # carries an empty workspace (it has no ``workspace_id`` yet), so building
        # ``merged`` from the commit payload silently dropped the real workspace
        # and the approval resume could not find it ("工作区路径不可用").
        if exec_cp.get("workspace"):
            merged["workspace"] = dict(exec_cp.get("workspace") or {})
        exec_timeline = [e for e in (exec_cp.get("timeline") or []) if isinstance(e, dict)]
        if exec_timeline:
            merged["timeline"] = exec_timeline
        if exec_cp.get("diff"):
            merged["diff"] = str(exec_cp.get("diff") or "")
        if isinstance(exec_cp.get("tests"), dict) and exec_cp.get("tests"):
            merged["tests"] = dict(exec_cp.get("tests") or {})
        if isinstance(exec_cp.get("summary"), dict) and exec_cp.get("summary"):
            merged["summary"] = dict(exec_cp.get("summary") or {})
    if commit_cp is not None:
        merged["committed"] = bool(commit_cp.get("committed"))
        # INC25 / T05 —— the commit payload's own approval state (``approved`` on a
        # granted resume) is carried forward so ``GET /runs/{id}``.codeplane.approval
        # reflects the decision the person just made; a payload that carries no
        # approval leaves the key absent (never invented).
        approval = commit_cp.get("approval")
        if isinstance(approval, dict) and approval:
            merged["approval"] = dict(approval)
    completed: list[str] = []
    affected: list[str] = []
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        label = str(step.get("note") or step.get("tool") or "")
        status = str(step.get("status") or "")
        if status == "ok":
            completed.append(label)
        elif status in ("unavailable", "error", "blocked"):
            affected.append(label)
    if completed:
        merged["completed_steps"] = completed
    if affected:
        merged["affected_steps"] = affected
    return merged


def _release_codeplane_workspace(
    run_id: str, codeplane_meta: dict[str, Any], *, destroy: bool
) -> None:
    """INC29 T01 (§7) — recycle a run's isolated workspace at the terminal state.

    Housekeeping, never a gate: a failure is logged and never disturbs the
    (already-terminal) run. The run's **own** workspace is released by
    ``run_id``; an approval **resume** run reuses the *original* run's workspace
    (so it is not bound to the new run id) and that workspace is recycled by the
    id the assembled payload still carries. ``destroy=False`` keeps the files for
    forensics; ``destroy=True`` removes them.
    """
    try:
        from forgeflow.codeplane.workspace import get_workspace_manager

        wm = get_workspace_manager()
    except Exception as exc:  # noqa: BLE001 — housekeeping must never break a run
        logger.warning("workspace manager unavailable for %s: %s", run_id, exc)
        return
    ws = wm.release_for_run(run_id, destroy=destroy)
    if ws is not None:
        return
    ws_id = str((codeplane_meta.get("workspace") or {}).get("workspace_id") or "")
    if not ws_id:
        return
    target = wm.get(ws_id)
    if target is None or target.state == "destroyed":
        return
    try:
        wm.release(ws_id, destroy=destroy)
    except Exception as exc:  # noqa: BLE001 — housekeeping must never break a run
        logger.warning("workspace release failed for %s: %s", ws_id, exc)


def _flag_llm_degradation(task: Any, errors: list[str]) -> None:
    """QA-INC37 realstack T4（INC25 纪律）：**显式降级**的运行不得自称干净成功。

    docs/sop/INC25-DESIGN.md：「引擎/模型不可用 = 显式降级 → degraded 非空 +
    运行**非「已完成」**；绝不静默 completed/success/errors=[]」。模型被配置了
    但构建成 mock（``provider_degraded_to_mock``）或调用抛异常
    （``exception: ...``）时，运行实际由确定性兜底完成 —— 必须在 ``errors``
    里留结构化痕迹，让 ``validate()`` 把终态判成非 success，否则 UI 会把
    "一次都没用上模型" 的运行展示成「已完成」。

    ``no_model`` 不算错误：那是离线确定性模式的**设计内路径**（未配置模型
    ≠ 故障），flag 它会把整套离线套件判红。
    """
    meta = task.context.get("llm_runtime")
    degraded = (
        str((meta or {}).get("degraded") or "").strip()
        if isinstance(meta, dict)
        else ""
    )
    if not degraded or degraded == "no_model":
        return
    msg = f"LLM 运行时显式降级（{degraded}）：本次运行由确定性兜底完成，模型未被实际使用"
    if msg not in errors:
        errors.append(msg)


async def run_task(
    task: TaskCreate,
    ctx: RequestContext,
    *,
    graph: Any | None = None,
    bus: RunEventBus | None = None,
    experience_repo: Any | None = None,
    policy_repo: Any | None = None,
    policy_engine: Any | None = None,
    run_id: str | None = None,
    thread_id: str | None = None,
    register_running: bool = False,
) -> RunHandle:
    """Execute a task to a terminal state and extract its experience.

    Returns a ``RunHandle`` whose ``detail`` carries outcome/steps/experience_id.

    INC32 (additive, default-safe): ``run_id`` / ``thread_id`` let the async
    dispatcher inject a pre-minted id (so the run is addressable before it
    starts) and ``register_running`` optionally pre-writes a ``running`` record
    up front. All three default to the pre-INC32 behaviour and every existing
    call site is unchanged byte-for-byte.
    """
    bus = bus or get_event_bus()
    exp_repo = experience_repo or get_experience_repository()
    pol_repo = policy_repo or get_policy_repository()

    run_id = run_id or new_id()
    thread_id = thread_id or new_id()
    # QA-INC37 realstack T3 —— 端到端耗时的**起点**。终态记录此前在收尾时才打
    # created_at ⇒ created_at == completed_at ⇒ 同步路径（POST /tasks 直接
    # await run_task）的任何运行时长恒为 0：有耗时数据显示成 0ms。在入口
    # 截获真实起点，与 INC32 ``build_running_record``"created_at 打一次"的
    # 约定对齐（预注册路径的 running 记录也是调度时刻的 created_at）。
    started_iso = datetime.now(timezone.utc).isoformat()
    # INC32 ADR-02 — resolve the workspace relationships. The parent is whatever
    # the caller declared under either key (``parent_run_id`` is the workspace
    # relationship the BFF sends; ``continued_from_run_id`` is the ADR-03
    # injection key — both honestly name the same parent run).
    _context = task.context or {}
    parent_run_id = str(
        _context.get("parent_run_id") or _context.get("continued_from_run_id") or ""
    )
    session_id = str(_context.get("session_id") or "")
    if not session_id and parent_run_id:
        # A Follow-up belongs to its parent's session by default (the first run
        # of a session defines it) — honest, and it keeps AC-39 true without the
        # caller having to echo the session id.
        parent = get_run_store().get(parent_run_id)
        if parent is not None:
            session_id = str(getattr(parent, "session_id", "") or "")
    session_id = session_id or run_id

    if register_running:
        register_running_run(
            run_id=run_id,
            thread_id=thread_id,
            tenant_id=ctx.tenant_id,
            intent=task.intent,
            workflow_type=task.workflow_type,
            session_id=session_id,
            parent_run_id=parent_run_id,
            actor_user_id=ctx.user_id,
            actor_role=ctx.role,
        )

    # --- A5 (INC2-10): the run's context comes from the platform builder. ---
    # This is what makes the recall → dedup → compress → rank pipeline real
    # instead of dead code: skills it selects become ctx.available_skills.
    bundle = None
    try:
        bundle = await _build_run_context(task, ctx, run_id)
        ctx.available_skills = [
            str(section.get("ref_id"))
            for section in bundle.sections
            if section.get("source") == "skill"
        ]
        # INC27 — project the SAME bundle for the code plane (same-source, so the
        # two planes can never rank differently). Memory sections already carry
        # id / content / scope; skills need the version+steps lookup above.
        ctx.injected_memory = [
            {
                "id": str(section.get("ref_id") or ""),
                "content": str(section.get("text") or ""),
                "scope": section.get("scope"),
                "similarity": float(section.get("similarity") or 0.0),
            }
            for section in bundle.sections
            if section.get("source") == "memory"
        ]
        ctx.injected_skills = await _resolve_injected_skills(
            ctx.tenant_id, ctx.available_skills
        )
    except Exception as exc:  # noqa: BLE001 — context is an enhancement, not a gate
        logger.warning("context build failed, continuing without it: %s", exc)

    # --- INC46 T12: the optional LangGraph Skill Subgraph (feature flag). ----
    # Additive & guarded. The flag is a pure ``os.environ`` read, default OFF.
    #
    # * Flag OFF (the default): ``skill_subgraph_enabled()`` is False, the branch
    #   below is skipped entirely, and the executor runs byte-for-byte as before
    #   (零回归 / 红线 7 — the flag is never read into any structure that alters
    #   the off path).
    # * Flag ON: each injected skill that declares a ``procedure`` is driven
    #   step-by-step through the shared-state subgraph (nodes gate via T21's
    #   pending store — pause/resume reuse it, there is no second mechanism),
    #   and the declared procedure is then removed from the context so the
    #   executor does not *also* drive it (a declared step runs exactly once).
    #
    # The seam sits here — at the real entry, where the chosen skill's procedure
    # is about to be driven — so every runtime mode (deterministic / llm / react
    # / graph) observes the flag. See ``forgeflow.skills.skill_subgraph``.
    from forgeflow.skills.skill_subgraph import skill_subgraph_enabled

    skill_subgraph_steps: list[dict[str, Any]] = []
    skill_subgraph_errors: list[str] = []
    if skill_subgraph_enabled():
        skill_subgraph_steps, skill_subgraph_errors = await _drive_selected_skill_subgraph(
            task, ctx, bus, run_id
        )
        _strip_skill_procedures(ctx)

    # --- INC4 §A: pick the executor. The Agent must really use the provider. ---
    # "llm" runs the LLM planning/reflection path; "deterministic" keeps the
    # original platform graph (offline default). ``_default_executor`` stays
    # reachable — see resolve_agent_runtime_mode().
    runtime_mode = resolve_agent_runtime_mode()
    executor: Any | None = None
    if graph is not None and hasattr(graph, "ainvoke"):
        runtime_mode = "graph"
        steps, errors = await _execute_graph(graph, task, ctx, bus, run_id)
    else:
        # INC17 — the executor is chosen by the resolved runtime mode. "react"
        # runs the multi-round tool-calling closed loop; "llm" keeps the pre-INC17
        # one-shot plan→execute→reflect path (reachable fallback); everything else
        # is the deterministic platform graph (the offline default). The react
        # executor is imported **inside the branch** so orchestrator and
        # react_executor never form a top-level import cycle.
        if runtime_mode == "react":
            from forgeflow.runtime.react_executor import react_executor

            executor = react_executor
        elif runtime_mode == "llm":
            executor = _llm_executor
        else:
            executor = _default_executor
        steps, errors = await executor(
            task, ctx, bus, run_id, policy_engine=policy_engine, attempt=0
        )
        _flag_llm_degradation(task, errors)

    # INC46 T12 — merge the subgraph-driven skill steps (empty with the flag off,
    # so this is a no-op then). Prepended so the deliverable (``report.render``)
    # stays last — the result-first invariant is preserved.
    if skill_subgraph_steps or skill_subgraph_errors:
        steps = [*skill_subgraph_steps, *steps]
        errors = [*skill_subgraph_errors, *errors]

    run_state: dict[str, Any] = {
        "run_id": run_id,
        "tenant_id": ctx.tenant_id,
        "team_id": ctx.team_id,
        "intent": task.intent,
        "workflow_type": task.workflow_type,
        "tags": [task.workflow_type],
        "steps": steps,
        "errors": errors,
        "status": "failed" if errors else "completed",
    }

    verdict = validate(run_state)
    status = "completed" if verdict.success else "failed"

    # Failure → replan loop (up to the configured ceiling), then HITL.
    # Two independent ceilings apply: the attempt count (``decide_replan``) and
    # the run's token / wall-clock budget (``LoopBreaker``, review finding #10).
    # The breaker is consulted only when a retry has *already* been authorised by
    # the count ceiling, so it can veto a retry but never authorise one.
    attempt = 0
    breaker = LoopBreaker()
    loop_started = time.monotonic()
    while not verdict.success:
        decision = decide_replan(verdict, attempt)
        await bus.emit(run_id, "replan", record_replan_event(decision))
        if decision.should_replan:
            breach = breaker.observe(
                tokens=_tokens_so_far(task),
                seconds=time.monotonic() - loop_started,
            )
            if breach is not None:
                # Budget exhausted: stop replanning and hand over to a human.
                errors.append(breach.message)
                run_state["errors"] = errors
                run_state["status"] = "failed"
                await bus.emit(run_id, "run.loop.breaker", breach.to_dict())
                # INC9 B4 / O1 — durable breadcrumb on the existing audit sink,
                # so "the loop stopped because X" survives a restart (the run
                # record alone does not — hub runs are in-process only).
                await _persist_loop_breaker_breadcrumb(ctx, run_id, breach, breaker)
                await _escalate_hitl(pol_repo, ctx, run_id, task, verdict)
                break
            attempt += 1
            # Re-run with the *same* executor that produced the first attempt
            # (the LLM path replans through the LLM too; the graph path keeps
            # its historical deterministic replan). The plan/reflection caches
            # make a replan of an identical plan free of extra LLM calls.
            retry_executor = executor or _default_executor
            steps_retry, errors = await retry_executor(
                task, ctx, bus, run_id, policy_engine=policy_engine, attempt=attempt
            )
            # 重试同样可能降级 —— 末次 attempt 的口径保持一致（设计 §10/§11-C）。
            _flag_llm_degradation(task, errors)
            # Keep the run's recorded steps in step with the attempt that actually
            # produced the terminal verdict (design §10/§11-C: "跨 replan steps 取
            # 末次 attempt"). Without this the module-level ``steps`` stays at the
            # *first* attempt's view while ``tool_invocations`` / ``plan`` already
            # reflect every round — the run-level 1:1 the T05 contract pins could
            # then never hold for any executor.
            steps = steps_retry
            run_state["steps"] = steps_retry
            run_state["errors"] = errors
            run_state["status"] = "failed" if errors else "completed"
            verdict = validate(run_state)
            continue
        if decision.escalate_hitl:
            await _escalate_hitl(pol_repo, ctx, run_id, task, verdict)
        break

    status = "completed" if verdict.success else "failed"

    # --- INC25 W2: the code-execution plane summary + the approval closure. ---
    # Assembled from the run's own code-plane invocations (never invented). When a
    # step is gated on a human decision the run status becomes ``awaiting_approval``
    # (never "已完成", AC-17) and a pending ``ApprovalRecord`` is raised so the
    # /approvals surface and the /codeplane decision endpoints agree (U5).
    codeplane_meta = _assemble_codeplane(
        list(task.context.get("tool_invocations") or []), steps
    )
    if _has_awaiting_approval(steps):
        status = "awaiting_approval"
        approval_meta: dict[str, Any] = {"status": "pending", "approval_id": None,
                                         "decided_by": None, "decided_at": None}
        try:
            from forgeflow.codeplane.approval import request_code_approval

            approval_record = await request_code_approval(
                pol_repo,
                tenant_id=ctx.tenant_id,
                run_id=run_id,
                requester=ctx.user_id,
                note=task.intent,
            )
            approval_meta = {
                "status": approval_record.status,
                "approval_id": approval_record.id,
                "decided_by": approval_record.approver,
                "decided_at": None,
            }
        except Exception as exc:  # noqa: BLE001 — HITL persistence must never break a run
            logger.warning("code-plane approval request failed: %s", exc)
        codeplane_meta["approval"] = approval_meta
    if codeplane_meta:
        task.context["codeplane"] = codeplane_meta

    # --- INC29 T01 (§7): recycle the run's isolated workspace at the terminal
    # state. A run paused for a human decision keeps its workspace (the approval
    # resume needs it); a run that finished — committed or died — releases it,
    # kept for forensics on a committed success and destroyed otherwise. A
    # non-code run has no codeplane payload and is left untouched.
    if codeplane_meta and status != "awaiting_approval":
        _release_codeplane_workspace(
            run_id,
            codeplane_meta,
            destroy=not (verdict.success and bool(codeplane_meta.get("committed"))),
        )

    # jump ② — always extract on a terminal state.
    experience = await ExperienceExtractor(repo=exp_repo).extract(
        run_state, verdict, tenant_id=ctx.tenant_id, team_id=ctx.team_id
    )

    # --- A1 cost producer: record the run's real token usage + cost. --------
    # The deterministic platform graph reports no usage, so a mock/self-hosted
    # deployment stays honestly cost-free; a caller that spent tokens reports
    # them on ``task.context["llm_usage"]``. This is what fills the cost ledger
    # the /cost/* endpoints and the home KPI #3 read.
    from forgeflow.observability.cost_tracker import CostTracker

    tracker = CostTracker(model=_default_usage_model())
    _record_usage(tracker, task.context.get("llm_usage"))
    cost_summary = tracker.summary()

    # Executor provenance is captured before the record is built so it lands in
    # the persisted run (and therefore in GET /runs/{id}) rather than only in
    # the transient SSE event. It stays ``None`` (not ``{}``) when absent: the
    # emitted-contract distinction is "no LLM provenance" vs "an empty record",
    # and callers/tests rely on it to tell the deterministic path apart.
    llm_meta = task.context.get("llm_runtime")
    # INC8 §3.3: additive plan provenance for the evaluation metrics (#3/#9).
    # Read from the LLM runtime meta when present; the deterministic path carries
    # no plan meta and is honestly reported as the "fallback" plan it is.
    _plan_meta = (llm_meta or {}).get("plan") if isinstance(llm_meta, dict) else None
    plan_source = (
        str((_plan_meta or {}).get("source") or "fallback")
        if isinstance(_plan_meta, dict)
        else "fallback"
    )
    # INC14 — one completion timestamp, shared by the record and by the
    # artifacts projection's fallback, so the two can never disagree.
    completed_iso = datetime.now(timezone.utc).isoformat()
    record = RunRecord(
        run_id=run_id,
        thread_id=thread_id,
        tenant_id=ctx.tenant_id,
        agent_id=task.context.get("agent_id"),
        intent=task.intent,
        status=status,
        outcome=verdict.outcome,
        steps=steps,
        errors=errors,
        created_at=started_iso,
        completed_at=completed_iso,
        experience_id=experience.id,
        total_tokens=int(cost_summary["total_tokens"]),
        total_cost_usd=float(cost_summary["total_cost_usd"]),
        cost_by_agent=dict(cost_summary["by_agent"]),
        runtime_mode=runtime_mode,
        llm=dict(llm_meta) if llm_meta else {},
        # Loop-breaker audit trail is captured *before* the record is built so
        # it lands on the run record — and therefore in GET /runs/{id} — rather
        # than only in the transient SSE event. NOTE (INC9 §B4): the hub run
        # store is process-lifetime and hub runs are NOT persisted, so this makes
        # the trip visible within the same process only — it is NOT a
        # cross-restart guarantee. The durable, cross-restart breadcrumb is the
        # audit-sink entry written at trip time (action="run.loop.breaker").
        loop=breaker.to_dict(),
        # INC8 §3.3 (additive): the skills this run selected, so per-task skill
        # reuse has a real source. Defaults keep every other path unchanged.
        plan_source=plan_source,
        skills_used=list(ctx.available_skills),
        # INC12 A1 (additive): every real tool invocation this run produced,
        # across all replan rounds. Filled from the cumulative
        # ``task.context["tool_invocations"]`` trail the executors write to.
        tool_invocations=list(task.context.get("tool_invocations") or []),
        # INC12 A5 (additive): the actor behind the request. ``ctx.user_id`` /
        # ``ctx.role`` were already resolved for the tool-call context and the
        # audit trail; writing them here too is what makes "谁发起的？"
        # answerable from ``GET /runs/{id}`` instead of only from audit logs.
        actor_user_id=ctx.user_id,
        actor_role=ctx.role,
        # INC14 (additive): the run's deliverables, projected from the run's own
        # real tool invocations. Empty when no successful report.render ran.
        artifacts=artifacts_from_invocations(
            run_id,
            list(task.context.get("tool_invocations") or []),
            fallback_created_at=completed_iso,
        ),
        # INC15 (additive): the run's Task Plan (L1) and its Observations (L3 —
        # the ``executed is True`` projection of the invocation trail). Both
        # default-safe, so a stray executor that wrote neither degrades to
        # ``{}`` / ``[]`` rather than breaking the record.
        plan=dict(task.context.get("plan") or {}),
        observations=_planning.observations_from_records(
            list(task.context.get("tool_invocations") or [])
        ),
        # INC22 W1 (additive): the run's **original declaration**, captured so a
        # manual replan can restore it. ``workflow_type`` is the task's real
        # domain (not the ``"generic"`` fallback replan used to leave behind) and
        # ``declared_inputs`` is exactly the explicit inputs the caller supplied —
        # never inferred, never completed (see the field docs on ``RunRecord``).
        workflow_type=task.workflow_type,
        declared_inputs=_declared_inputs(task),
        # INC25 W2 (additive): the code-execution plane summary (engine /
        # degraded / workspace / timeline / tests / diff / approval). Empty on a
        # non-code run, so GET /runs/{id} degrades honestly to ``{}``.
        codeplane=dict(task.context.get("codeplane") or {}),
        # INC32 ADR-02 (additive): the workspace relationships, so
        # ``GET /runs/{id}`` and the session grouping can resolve them.
        session_id=session_id,
        parent_run_id=parent_run_id,
    )
    get_run_store().save(record)

    # INC32 ADR-02 — persist the header / relationship / artifacts projection.
    # Best-effort: the in-process record above stays authoritative for
    # ``GET /runs/{id}``; this is what survives a restart.
    await persist_workspace_record(record)

    await bus.emit(
        run_id,
        "run.completed" if verdict.success else "run.failed",
        {
            "status": status,
            "outcome": verdict.outcome,
            "experience_id": experience.id,
            "steps": len(steps),
            "errors": errors,
            "total_tokens": record.total_tokens,
            "total_cost_usd": record.total_cost_usd,
            "runtime_mode": runtime_mode,
            "loop": breaker.to_dict(),
            "tool_invocations": list(record.tool_invocations),
            "artifacts": list(record.artifacts),
            "plan": dict(record.plan),
            "observations": list(record.observations),
            "codeplane": dict(record.codeplane),
        },
    )

    return RunHandle(
        run_id=run_id,
        thread_id=thread_id,
        status=status,
        detail={
            "outcome": verdict.outcome,
            "experience_id": experience.id,
            "steps": steps,
            "errors": errors,
            "replans": attempt,
            "total_tokens": record.total_tokens,
            "total_cost_usd": record.total_cost_usd,
            "runtime_mode": runtime_mode,
            "llm": llm_meta,
            "loop": breaker.to_dict(),
            "tool_invocations": list(record.tool_invocations),
            "artifacts": list(record.artifacts),
            "plan": dict(record.plan),
            "observations": list(record.observations),
            "codeplane": dict(record.codeplane),
        },
        session_id=session_id,
        parent_run_id=parent_run_id,
    )


async def _escalate_hitl(
    policy_repo: Any,
    ctx: RequestContext,
    run_id: str,
    task: TaskCreate,
    verdict: Any,
) -> None:
    """Create a high-risk approval when a failed run exhausts its replans."""
    from forgeflow.governance.models import ApprovalRecord

    approval = ApprovalRecord(
        tenant_id=ctx.tenant_id,
        run_id=run_id,
        risk_level="high",
        requested_action=f"replan-exhausted: {task.intent or task.workflow_type}",
        requester=ctx.user_id,
        status="pending",
        note="; ".join(verdict.reasons),
    )
    try:
        await policy_repo.save_approval(approval)
    except Exception:  # noqa: BLE001 — HITL persistence must never break the run
        pass
