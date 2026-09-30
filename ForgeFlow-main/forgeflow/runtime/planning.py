"""Task planning — the L1 (Task Plan) layer of the four-layer contract (INC15).

Why this module exists
----------------------
Before INC15 the runtime walked a **hard-coded** ``_DEFAULT_STEPS`` constant and
force-fed every task the same four steps. A plain intent was therefore pushed
through ``data.query`` / ``code.run`` even though it had neither a table name nor
a repository path — those steps could never run, so they were recorded as
``skipped`` and the report claimed a completeness the run never had. Two defects
followed:

1. **No dynamic plan** — the plan was a constant, not a function of the task.
2. **The four-layer contract was missing** — there was no distinct *Task Plan*
   layer, no *Observation* projection and no honest way for the *Report* to say
   "this step did not apply" / "this step was blocked for a stated reason".

This module is the pure, side-effect-free planner. It turns a
:class:`CapabilityContext` (the real signals the runtime can derive) into a
:class:`TaskPlan` with three honest outcomes per candidate step:

* **required** — the step has all its real inputs and must run;
* **blocked** — the step was *declared* (a skill / the model named it) but a
  required input is missing; it is kept in the plan (never silently dropped) with
  a human ``blocked_reason``;
* **not_applicable** — the step is irrelevant to this task (no input and not
  declared); it is trimmed into ``TaskPlan.not_applicable`` (it never becomes an
  execution record).

Design guarantees (the honest-planning contract; the tests pin every row)
--------------------------------------------------------------------------
* **No invented inputs.** ``table`` / ``paths`` / ``repo_path`` may only come from
  ``explicit_inputs`` — the planner never guesses a table name or a path.
  ``query`` / ``text`` may be derived from the real user ``intent`` text.
* **No fuzzy tool selection.** The ``intent`` string is used *only* to fill the
  ``query`` / ``text`` value — never to keyword-match a tool. The only signals
  that select a tool are ``explicit_inputs`` ∪ ``declared_tools`` ∪
  ``workflow_type``.
* **Product-step integrity.** :data:`REPORT_TOOL` (``report.render``), when it is
  among the candidates, is always emitted **last** and is never trimmed as
  not-applicable — it is the L4 (Report) producer and the run's only deliverable.
* **Stable join key.** Every :class:`PlanStep` carries
  ``step_id == "{run_id}:{attempt}:{index}"``, the same key used by the
  execution record (L2) and the observation (L3).

The module is deliberately **pure and IO-free** so every rule below is a unit
test: it imports nothing from ``orchestrator`` / ``llm_planner`` (which prevents
an import cycle) and never touches settings, storage or the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "REPORT_TOOL",
    "TOOL_ORDER",
    "TOOL_INPUT_CONTRACT",
    "_DEFAULT_CANDIDATE_TOOLS",
    "LEGACY_STATUS_ALIASES",
    "SUCCEEDED_STATUS",
    "FAILED_STATUSES",
    "EXECUTED_STATUSES",
    "TERMINAL_STATUSES",
    "CapabilityContext",
    "PlanStep",
    "NotApplicableStep",
    "TaskPlan",
    "applicability",
    "blocked_reason",
    "resolve_inputs",
    "build_plan",
    "plan_from_records",
    "normalize_status",
    "observations_from_records",
]

# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #
#: The one plan tool whose successful invocation becomes the run's deliverable
#: (the L4 Report producer — see :mod:`forgeflow.runtime.artifacts`).
REPORT_TOOL = "report.render"

#: Dependency order for the shipped plan tools. ``report.render`` is always last
#: (it renders the plan + records that precede it). Used to give a deterministic
#: order to whatever subset of candidates is applicable.
TOOL_ORDER: tuple[str, ...] = (
    "research.search",
    "docs.parse",
    "data.query",
    "git.diff",
    "code.lint",
    # INC25 W2 — the real code-execution steps. ``code.execute`` drives the
    # isolated workspace + engine (produces the diff / test evidence) and
    # ``code.commit`` is the human-in-the-loop gate that sits **right after it**
    # (design §3.3 #32: "code.execute 在 code.run 前、code.commit 紧邻其后").
    # ``code.run`` keeps its permanent, unchanged meaning (ast validation only).
    "code.execute",
    "code.commit",
    "code.run",
    "analysis.score",
    "report.render",
)

#: The default (deterministic) candidate set. The name ``_DEFAULT_STEPS`` is kept
#: in ``orchestrator`` for the tests that monkeypatch it; this is the tool-name
#: form the planner works with. Note that ``code.lint`` / ``git.diff`` /
#: ``docs.parse`` / ``analysis.score`` are deliberately **not** in the default
#: candidates — they are only enabled when a skill declares them or the caller
#: supplies their real input (keeps the offline plan lean and dependency-free).
_DEFAULT_CANDIDATE_TOOLS: tuple[str, ...] = (
    "research.search",
    "data.query",
    "code.run",
    "report.render",
)

#: Per-tool required inputs and which of them may be *derived* from the real
#: user intent text. A ``table`` / ``path`` / ``repo_path`` is never derivable —
#: guessing one would be fabrication. Assertable by tests.
TOOL_INPUT_CONTRACT: dict[str, dict[str, tuple[str, ...]]] = {
    "research.search": {"required": ("query",), "derivable": ("query",)},
    "docs.parse": {"required": ("text",), "derivable": ("text",)},
    "data.query": {"required": ("table",), "derivable": ()},
    "git.diff": {"required": ("repo_path",), "derivable": ()},
    "code.lint": {"required": ("paths",), "derivable": ()},
    "code.run": {"required": ("paths",), "derivable": ()},
    # INC25 W2 — ``code.execute`` needs a real code input (``paths`` /
    # ``repo_path``, which ``resources`` dereference supplies) before it may run
    # the engine on a workspace; without it the step is trimmed, never run on a
    # guessed path. ``code.commit`` takes **no** external input — it is the
    # approval gate over the workspace ``code.execute`` produced, so it is always
    # "required" when it is a candidate.
    "code.execute": {"required": ("paths",), "derivable": ()},
    "code.commit": {"required": (), "derivable": ()},
    "analysis.score": {"required": ("observations",), "derivable": ()},
    "report.render": {"required": (), "derivable": ()},
}

# --------------------------------------------------------------------------- #
# Status model                                                                 #
# --------------------------------------------------------------------------- #
#: ``skipped`` is **retired** as a writer status (INC15): the executor now emits
#: ``blocked`` for "needed but missing input". A reader (report / frontend /
#: aggregation) maps the legacy value back so an old trail / export still renders
#: honestly.
LEGACY_STATUS_ALIASES: dict[str, str] = {"skipped": "blocked"}

#: Statuses the writer may record as a terminal execution-record state.
SUCCEEDED_STATUS = "ok"
FAILED_STATUSES: frozenset[str] = frozenset({"error", "unavailable", "refused"})
BLOCKED_STATUS = "blocked"
NOT_APPLICABLE_STATUS = "not_applicable"
#: Statuses that mean a handler actually ran (the L3 Observation predicate).
EXECUTED_STATUSES: frozenset[str] = frozenset({"ok", "error"})
#: Every terminal status the writer may produce on an execution record.
TERMINAL_STATUSES: frozenset[str] = (
    frozenset({SUCCEEDED_STATUS}) | FAILED_STATUSES | frozenset({BLOCKED_STATUS})
)


def normalize_status(raw: str | None) -> str:
    """Map a (possibly legacy) status string onto the current wire vocabulary.

    The only migration today is ``skipped → blocked`` (INC15 retires the writer
    but keeps the reader compatible so a historical run / export still renders
    with the honest "受阻" wording rather than the retired one).
    """
    return LEGACY_STATUS_ALIASES.get(str(raw or "").strip().lower(), str(raw or "").strip().lower())


# --------------------------------------------------------------------------- #
# Value objects                                                                #
# --------------------------------------------------------------------------- #
@dataclass
class CapabilityContext:
    """The real signals the planner is allowed to use to build a plan.

    Attributes:
        intent: the user's task intent (used only for ``query`` / ``text``).
        workflow_type: the workflow template this task was submitted as.
        explicit_inputs: real inputs the caller supplied (``table`` / ``paths`` /
            ``repo_path`` / ``query`` / ``text`` …). The **only** legal source for
            a table name or a path.
        declared_tools: tools a selected skill / workflow (or the LLM) explicitly
            named. Declared tools are kept even when their input is missing (as
            ``blocked``) instead of being trimmed.
        available_skills: the skills the run selected (context only).
        has_prior_observations: whether any L2 record already executed (drives
            ``analysis.score`` applicability).
        observations: the real prior L2 records (the ``observations`` input for
            ``analysis.score`` / ``report.render``).
    """

    intent: str = ""
    workflow_type: str = "generic"
    explicit_inputs: dict[str, Any] = field(default_factory=dict)
    declared_tools: list[str] = field(default_factory=list)
    available_skills: list[str] = field(default_factory=list)
    has_prior_observations: bool = False
    observations: list[dict[str, Any]] = field(default_factory=list)
    #: INC32 ADR-03 (additive, default-safe) — the Follow-up parent-run summary,
    #: dereferenced from a declared ``continued_from_run_id`` by
    #: ``orchestrator._resolve_continued_context``. It is a **derived view** of
    #: the parent run, never a caller-declared input, so it is deliberately NOT
    #: part of ``explicit_inputs`` (the "declared_inputs == planner's explicit
    #: inputs" invariant is preserved). Empty for every plain task, so the
    #: planner's behaviour is byte-for-byte unchanged in that case. The runtime
    #: appends it after the executor system prompt (never a tool-selection signal).
    prior_context: str = ""


@dataclass
class NotApplicableStep:
    """A candidate step that does not apply to this task (L1 only — never run)."""

    tool: str
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"tool": self.tool, "reason": self.reason}


@dataclass
class PlanStep:
    """One planned step of a :class:`TaskPlan`."""

    step_id: str
    index: int
    tool: str
    note: str = ""
    step_type: str = "agent"
    #: ``"required"`` or ``"blocked"`` (``not_applicable`` steps are not steps).
    applicability: str = "required"
    required_inputs: list[str] = field(default_factory=list)
    args: dict[str, Any] = field(default_factory=dict)
    #: Non-empty only when ``applicability == "blocked"`` — the stated reason.
    blocked_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON form for the ``plan`` block on the run (args are omitted — they
        carry the (potentially large) observation list and are execution detail,
        not plan detail)."""
        return {
            "step_id": self.step_id,
            "index": self.index,
            "tool": self.tool,
            "note": self.note,
            "step_type": self.step_type,
            "applicability": self.applicability,
            "required_inputs": list(self.required_inputs),
            "blocked_reason": self.blocked_reason,
        }

    def to_payload(self, index: int | None = None, *, status: str = "ok") -> dict[str, Any]:
        """The ``RunRecord.steps[]`` entry for this step (1:1 with its record).

        ``blocked_reason`` is included **verbatim** (same value as :meth:`to_dict`)
        so ``steps[].blocked_reason`` and ``plan.steps[].blocked_reason`` can never
        disagree (INC23): a blocked step carries the same stated reason on both
        layers, and a non-blocked step carries ``""`` on both.
        """
        return {
            "tool": self.tool,
            "step_type": self.step_type,
            "note": self.note,
            "index": self.index if index is None else index,
            "status": status,
            "step_id": self.step_id,
            "applicability": self.applicability,
            "blocked_reason": self.blocked_reason,
        }


@dataclass
class TaskPlan:
    """The plan for one run attempt (L1)."""

    run_id: str = ""
    attempt: int = 0
    source: str = "deterministic"
    reasoning: str = ""
    steps: list[PlanStep] = field(default_factory=list)
    not_applicable: list[NotApplicableStep] = field(default_factory=list)

    def summary(self, records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Counts for the run's ``plan.summary`` block.

        ``planned`` / ``not_applicable`` come from the plan itself; the execution
        counts are **recomputed from the L2 records** when supplied (never summed
        over a mixed collection). ``blocked`` counts plan-level blocked steps when
        no records are given (so a plan-only view is still honest).
        """
        recs = [r for r in (records or []) if isinstance(r, dict)]
        planned = len(self.steps)
        not_applicable = len(self.not_applicable)
        if recs:
            executed = sum(1 for r in recs if r.get("executed") is True)
            succeeded = sum(1 for r in recs if r.get("status") == SUCCEEDED_STATUS)
            blocked = sum(
                1 for r in recs if normalize_status(r.get("status")) == BLOCKED_STATUS
            )
            failed = sum(
                1 for r in recs if normalize_status(r.get("status")) in FAILED_STATUSES
            )
        else:
            executed = succeeded = failed = 0
            blocked = sum(1 for s in self.steps if s.applicability == "blocked")
        return {
            "planned": planned,
            "executed": executed,
            "succeeded": succeeded,
            "blocked": blocked,
            "failed": failed,
            "not_applicable": not_applicable,
        }

    def to_dict(self, records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "attempt": self.attempt,
            "source": self.source,
            "reasoning": self.reasoning,
            "steps": [s.to_dict() for s in self.steps],
            "not_applicable": [n.to_dict() for n in self.not_applicable],
            "summary": self.summary(records),
        }


# --------------------------------------------------------------------------- #
# Input resolution                                                             #
# --------------------------------------------------------------------------- #
_MISSING_LABELS: dict[str, str] = {
    "query": "查询词(query)",
    "text": "文本(text)",
    "table": "表名(table)",
    "paths": "路径(paths/repo_path)",
    "repo_path": "仓库路径(repo_path)",
    "observations": "观测记录(observations)",
}


def _first_str(value: Any) -> str:
    """First non-empty string from a scalar / list input (``""`` when none)."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        for item in value:
            text = str(item or "").strip()
            if text:
                return text
    return ""


def _path_inputs(explicit: dict[str, Any]) -> tuple[list[str], str]:
    """Return ``(paths, repo_path)`` from the explicit inputs, normalised."""
    paths: list[str] = []
    raw = explicit.get("paths")
    if isinstance(raw, (list, tuple)):
        paths.extend(str(p) for p in raw if str(p or "").strip())
    elif isinstance(raw, str) and raw.strip():
        paths.append(raw.strip())
    repo = explicit.get("repo_path")
    repo_path = repo.strip() if isinstance(repo, str) and repo.strip() else ""
    return paths, repo_path


def resolve_inputs(tool: str, ctx: CapabilityContext) -> tuple[dict[str, Any], list[str]]:
    """Resolve ``tool``'s real inputs from ``ctx``.

    Returns ``(args, missing)`` where ``missing`` lists the required input keys
    that could not be resolved. The base ``args`` always carries
    ``text`` / ``intent`` / ``query`` / ``observations`` (the runtime's
    backwards-compatible argument bag); a tool-specific key (``table`` /
    ``paths`` / ``repo_path``) is added only when it was really supplied.

    Only ``explicit_inputs`` may supply ``table`` / ``paths`` / ``repo_path``;
    ``query`` / ``text`` fall back to the real intent text.
    """
    intent = str(ctx.intent or "")
    explicit = dict(ctx.explicit_inputs or {})
    query = _first_str(explicit.get("query")) or intent
    text = _first_str(explicit.get("text")) or _first_str(explicit.get("documents")) or intent
    observations = [r for r in (ctx.observations or []) if isinstance(r, dict)]

    args: dict[str, Any] = {
        "text": text,
        "intent": intent,
        "query": query,
        "observations": observations,
    }
    missing: list[str] = []

    if tool == "data.query":
        table = _first_str(explicit.get("table"))
        if table:
            args["table"] = table
        else:
            missing.append("table")
    elif tool in ("code.run", "code.lint", "code.execute"):
        paths, repo_path = _path_inputs(explicit)
        if paths:
            args["paths"] = paths
        if repo_path:
            args["repo_path"] = repo_path
        if not paths and not repo_path:
            missing.append("paths")
    elif tool == "git.diff":
        _, repo_path = _path_inputs(explicit)
        if repo_path:
            args["repo_path"] = repo_path
        else:
            missing.append("repo_path")
    elif tool == "docs.parse":
        if not text.strip():
            missing.append("text")
    elif tool == "research.search":
        if not query.strip():
            missing.append("query")
    elif tool == "analysis.score":
        if not observations:
            missing.append("observations")
    # report.render needs no external input — it is always renderable.
    return args, missing


def applicability(tool: str, ctx: CapabilityContext) -> str:
    """Classify ``tool`` for ``ctx``: ``required`` / ``blocked`` / ``not_applicable``.

    ``report.render`` is always ``required`` (the product step). Otherwise a tool
    with an unresolved required input is ``blocked`` when it was **declared**
    (a skill / the model named it) and ``not_applicable`` when it was not — the
    "declare it and it is kept (blocked); do not, and it is trimmed" rule.
    """
    if tool == REPORT_TOOL:
        return "required"
    _, missing = resolve_inputs(tool, ctx)
    if not missing:
        return "required"
    if tool in set(ctx.declared_tools or ()):
        return "blocked"
    return "not_applicable"


def blocked_reason(tool: str, ctx: CapabilityContext) -> str:
    """The human reason a ``blocked`` step cannot run (``""`` when it is not)."""
    if tool == REPORT_TOOL:
        return ""
    _, missing = resolve_inputs(tool, ctx)
    if not missing:
        return ""
    if tool not in set(ctx.declared_tools or ()):
        return ""
    labels = "、".join(_MISSING_LABELS.get(key, key) for key in missing)
    return f"缺少必需输入：{labels}；该步已受阻，等待补充输入"


def _not_applicable_reason(tool: str, ctx: CapabilityContext) -> str:
    _, missing = resolve_inputs(tool, ctx)
    if not missing:
        return f"与当前任务无关：{tool} 不在本任务的候选步骤内"
    labels = "、".join(_MISSING_LABELS.get(key, key) for key in missing)
    return f"与当前任务无关：未提供 {labels}，该步不属于本任务"


# --------------------------------------------------------------------------- #
# Plan building                                                                #
# --------------------------------------------------------------------------- #
def _candidate_tools(candidates: Any) -> list[str]:
    """Normalise candidates (dicts or strings) into an ordered, de-duplicated list."""
    tools: list[str] = []
    for item in candidates or []:
        if isinstance(item, dict):
            tool = str(item.get("tool") or "").strip()
        else:
            tool = str(item or "").strip()
        if tool and tool not in tools:
            tools.append(tool)
    return tools


def _candidate_meta(candidates: Any) -> tuple[dict[str, str], dict[str, str]]:
    """Extract ``(notes, step_types)`` from the candidate dicts."""
    notes: dict[str, str] = {}
    step_types: dict[str, str] = {}
    for item in candidates or []:
        if isinstance(item, dict):
            tool = str(item.get("tool") or "").strip()
            if not tool:
                continue
            notes.setdefault(tool, str(item.get("note") or ""))
            step_types.setdefault(tool, str(item.get("step_type") or "agent"))
    return notes, step_types


def build_plan(
    candidates: Any,
    ctx: CapabilityContext,
    *,
    run_id: str = "",
    attempt: int = 0,
    source: str = "deterministic",
    reasoning: str = "",
) -> TaskPlan:
    """Build the :class:`TaskPlan` for ``candidates`` under ``ctx``.

    Steps emitted are every candidate whose :func:`applicability` is ``required``
    or ``blocked`` (blocked steps are kept, with a ``blocked_reason``); every
    ``not_applicable`` candidate goes into ``not_applicable`` instead. The order
    is :data:`TOOL_ORDER`, with any unknown candidate appended, and
    :data:`REPORT_TOOL` always forced to the end when it is a candidate.
    """
    tools = _candidate_tools(candidates)
    notes, step_types = _candidate_meta(candidates)

    ordered = [t for t in TOOL_ORDER if t in tools]
    ordered += [t for t in tools if t not in TOOL_ORDER]
    # The product step is always last (when it is a candidate).
    if REPORT_TOOL in ordered:
        ordered = [t for t in ordered if t != REPORT_TOOL] + [REPORT_TOOL]

    steps: list[PlanStep] = []
    not_applicable: list[NotApplicableStep] = []
    for tool in ordered:
        verdict = applicability(tool, ctx)
        if verdict == "not_applicable":
            not_applicable.append(
                NotApplicableStep(tool=tool, reason=_not_applicable_reason(tool, ctx))
            )
            continue
        index = len(steps)
        args, missing = resolve_inputs(tool, ctx)
        steps.append(
            PlanStep(
                step_id=f"{run_id}:{attempt}:{index}",
                index=index,
                tool=tool,
                note=notes.get(tool, ""),
                step_type=step_types.get(tool, "agent"),
                applicability=verdict,
                required_inputs=list(TOOL_INPUT_CONTRACT.get(tool, {}).get("required", ())),
                args=args,
                blocked_reason=blocked_reason(tool, ctx) if verdict == "blocked" else "",
            )
        )

    plan = TaskPlan(
        run_id=run_id,
        attempt=attempt,
        source=source,
        reasoning=reasoning,
        steps=steps,
        not_applicable=not_applicable,
    )
    # Invariant: the deliverable step, when planned, is always last.
    if steps:
        assert REPORT_TOOL not in tools or steps[-1].tool == REPORT_TOOL, (
            "build_plan must place report.render last"
        )
    return plan


def _record_blocked_reason(record: dict[str, Any]) -> str:
    """A ``blocked`` L2 record's stated reason, **verbatim** (``""`` when none).

    ``tool_executor`` writes the caller's reason into both ``payload["reason"]``
    and ``error`` (the same value); prefer the payload copy, fall back to
    ``error``. A missing / empty / non-string value yields ``""`` — the reason is
    **never invented**, so a blocked step with no recorded reason stays honest.
    """
    payload = record.get("payload")
    if isinstance(payload, dict):
        reason = payload.get("reason")
        if isinstance(reason, str) and reason.strip():
            return reason
    error = record.get("error")
    if isinstance(error, str) and error.strip():
        return error
    return ""


def plan_from_records(
    records: list[dict[str, Any]] | None,
    *,
    run_id: str = "",
    attempt: int = 0,
    source: str = "react",
    reasoning: str = "",
    notes: dict[int, str] | None = None,
) -> TaskPlan:
    """Project the L2 execution records into the L1 plan at **strict 1:1** (INC17).

    One record ⇒ one :class:`PlanStep` (``index`` = the record's position), so
    ``len(plan.steps) == len(records)`` holds **even when the model calls the
    same tool more than once**. This is deliberately **not** :func:`build_plan`:

      * ``build_plan`` de-duplicates its candidates (``_candidate_tools``) and
        forces ``report.render`` to the tail, so it can never represent a real
        tool sequence with repeats — it would collapse two ``research.search``
        calls into one and break the 1:1 contract the four-layer model relies on.
      * this function preserves **order and duplicates**, never reorders and never
        trims. ``not_applicable`` is always ``[]`` — a recorded step *happened*, so
        it is never "not applicable".

    ``step_id`` is taken from the record when present (the runtime stamps every
    record with ``f"{run_id}:{attempt}:{index}"``), otherwise it falls back to the
    same formula so the plan and the record still join on the stable key.

    ``applicability`` mirrors the record's status (INC23 — it no longer claims
    ``"required"`` for a step that provably did not run): a ``blocked`` record
    (``normalize_status(record["status"]) == BLOCKED_STATUS``) is projected as
    ``"blocked"`` with its stated ``blocked_reason`` carried out **verbatim**
    (``payload["reason"]`` first, else ``error``; ``""`` when neither). Every
    other record stays ``"required"``. This aligns the ReAct projection with
    :func:`build_plan`, which already marks a declared-but-under-specified step
    ``"blocked"`` — one semantic and one vocabulary across **both** runtime modes.
    """
    recs = [r for r in (records or []) if isinstance(r, dict)]
    note_map = notes or {}
    steps: list[PlanStep] = []
    for index, record in enumerate(recs):
        raw_step_id = record.get("step_id")
        step_id = (
            raw_step_id
            if isinstance(raw_step_id, str) and raw_step_id
            else f"{run_id}:{attempt}:{index}"
        )
        is_blocked = normalize_status(record.get("status")) == BLOCKED_STATUS
        steps.append(
            PlanStep(
                step_id=step_id,
                index=index,
                tool=str(record.get("tool") or "").strip(),
                note=str(note_map.get(index, "") or ""),
                step_type="agent",
                applicability="blocked" if is_blocked else "required",
                blocked_reason=_record_blocked_reason(record) if is_blocked else "",
            )
        )
    return TaskPlan(
        run_id=run_id,
        attempt=attempt,
        source=source,
        reasoning=reasoning,
        steps=steps,
        not_applicable=[],
    )


# --------------------------------------------------------------------------- #
# Observation projection (L3)                                                  #
# --------------------------------------------------------------------------- #
def observations_from_records(records: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Project the L2 execution records onto the L3 observation view.

    An observation exists **only** for a step that really executed
    (``executed is True``). Each is a verbatim copy of its record, so the
    ``observations == [r for r in records if r["executed"]]`` invariant holds and
    a projection never rewrites a field.
    """
    out: list[dict[str, Any]] = []
    for record in records or []:
        if isinstance(record, dict) and record.get("executed") is True:
            out.append(dict(record))
    return out
