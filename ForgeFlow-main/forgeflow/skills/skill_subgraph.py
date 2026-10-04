"""INC46 T12 — the optional LangGraph **Skill Subgraph** (feature-flagged).

What this module is
-------------------
Skill execution can be run through a *compiled LangGraph subgraph* instead of a
plain inline loop. The subgraph is **optional** and **default-OFF**: with the
flag off, :func:`execute_skill_run` dispatches to the inline reference path and
the LangGraph machinery is never touched (零回归 — the off-path is byte-for-byte
the "打开前" behaviour).

The subgraph **shares the run's state** — it does *not* hold an independent copy.
The single source of truth is :class:`SkillRunState`; the caller (the run's "主图"
owner) constructs it, and the subgraph stores **the same object reference**
(``SkillSubgraph.state is main_state``). Every LangGraph node mutates that object
in place, so a run can never end with two disagreeing truths
(:func:`reconcile`).

HITL is **not re-implemented**
------------------------------
Every "the run must stop and wait for a person" moment goes through the T21
primitive (:mod:`forgeflow.hitl.pending` + :mod:`forgeflow.hitl.policy`): the
subgraph calls :func:`forgeflow.hitl.policy.plan_for_tool` to classify a step's
tool (``READ/WRITE/EXTERNAL/DANGEROUS``) and persists a
:class:`~forgeflow.hitl.pending.PendingAction` via the existing
:func:`~forgeflow.hitl.pending.get_pending_store`. Resume re-reads *that* store —
there is no second pause mechanism (红线 21). An unresolved / expired / cancelled
pending **never** lets the run continue.

Red lines honoured here
-----------------------
* **18** — the execution loop has an explicit budget
  (:data:`DEFAULT_MAX_ITERATIONS` / the ``max_iterations`` argument) and a hard
  termination: exceeding it halts with ``status="halted"``; a tool is never
  driven twice for the same cursor.
* **21** — no step bypasses HITL: RBAC + the platform whitelist are re-checked
  immediately before a step runs, and a policy ``deny`` produces ``status="denied"``
  with **no** execution.
* **召回唯一路径** — this module never opens a recall path. It consumes a
  *already-selected* procedure (``spec["procedure"]`` via T03's
  :func:`forgeflow.skills.runtime.load_procedure`); the only context-recall entry
  point stays ``forgeflow.experience.context_builder.build_context``.

Why the feature flag lives in this module (not ``forgeflow/config.py``)
-----------------------------------------------------------------------
:func:`skill_subgraph_enabled` reads the environment variable
``FORGEFLOW_SKILL_SUBGRAPH`` directly and defaults to **False**. It is
deliberately **not** added to ``forgeflow/config.py``:

1. **Concurrency safety** — this task runs alongside other in-flight tasks that
   edit ``config.py``; a module-local reader avoids a shared-file edit and the
   churn a new ``Settings`` field would force on every construction site.
2. **Default-OFF needs no config surface** — the subgraph is experimental and
   off by default, so no operator has to configure anything to keep today's
   behaviour; an opt-in env var is the smallest possible surface.
3. The repo has no generic env-flag helper to reuse (the closest,
   ``forgeflow.runtime.trace_store.trace_persistence_enabled``, is
   backend-derived, not a generic reader), so a tiny local reader is the honest
   choice.

Honest degradation (红线 10)
----------------------------
If ``langgraph`` is not importable the subgraph does **not** pretend to run: the
on-path returns a result with ``path="skipped"`` and
``status="skipped"`` (reason names the missing dependency) and leaves the shared
state untouched. It never fabricates a successful run. Likewise a step with no
injected ``runner`` is reported as ``unavailable`` rather than a fake success.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from typing_extensions import TypedDict

from forgeflow.hitl import policy as hitl_policy
from forgeflow.hitl.pending import (
    ACTION_AUTO,
    ACTION_CONFIRM_PERMISSION,
    ACTION_DENY,
    ACTION_PENDING_APPROVAL,
    CANCELLED,
    EXPIRED,
    RESOLVED,
    WAITING,
    PendingActionStore,
    get_pending_store,
)
from forgeflow.runtime.gate import (
    PLATFORM_PLAN_TOOLS,
    check_tool_permission,
    describe_denial,
)
from forgeflow.skills.runtime import SkillStep, load_procedure
from forgeflow.skills.tool_permissions import classify_tool

logger = logging.getLogger(__name__)

__all__ = [
    # feature flag
    "FEATURE_FLAG_ENV",
    "skill_subgraph_enabled",
    # langgraph probe
    "langgraph_available",
    "langgraph_version",
    "LangGraphUnavailable",
    # paths / statuses
    "PATH_INLINE",
    "PATH_GRAPH",
    "PATH_SKIPPED",
    "RUNNING",
    "PAUSED",
    "DONE",
    "DENIED",
    "EXPIRED_STATUS",
    "CANCELLED_STATUS",
    "FAILED",
    "HALTED",
    "SKIPPED",
    "TERMINAL_STATUSES",
    "DEFAULT_MAX_ITERATIONS",
    # state + result
    "SkillRunState",
    "SkillRunResult",
    "SkillSubgraph",
    "StepRunner",
    "steps_from_spec",
    # entry points
    "execute_skill_run",
    "run_skill_subgraph",
    "reconcile",
    # graph-run counter (test observability, mirrors a spy)
    "graph_run_count",
    "reset_graph_run_count",
]

# --------------------------------------------------------------------------- #
# Feature flag (module-local by design — see module docstring)                 #
# --------------------------------------------------------------------------- #
#: The environment variable that opts a process into the LangGraph skill subgraph.
FEATURE_FLAG_ENV = "FORGEFLOW_SKILL_SUBGRAPH"
#: The raw values read as "on" (case-insensitive). Everything else — including the
#: default and the empty string — is "off".
_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})


def skill_subgraph_enabled() -> bool:
    """Whether the LangGraph skill subgraph is enabled in this process.

    Reads ``os.environ.get("FORGEFLOW_SKILL_SUBGRAPH", "0")`` **at call time**
    (never cached, so a test/operator can flip it per-process) and returns
    ``True`` only for ``{"1","true","yes","on"}`` (case-insensitive). The default
    is ``False`` — the subgraph is opt-in and OFF unless explicitly enabled.

    See the module docstring for why this is a module-local env reader rather
    than a ``forgeflow.config`` setting.
    """
    raw = os.environ.get(FEATURE_FLAG_ENV, "0")
    return str(raw).strip().lower() in _TRUTHY


# --------------------------------------------------------------------------- #
# LangGraph availability probe (honest degradation — 红线 10)                  #
# --------------------------------------------------------------------------- #
try:  # pragma: no cover — exercised by the availability probe below
    from langgraph.graph import END as _LG_END
    from langgraph.graph import StateGraph as _LG_StateGraph

    _LANGGRAPH_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001 — a missing optional dep must degrade, not crash
    _LG_END = "__end__"  # type: ignore[assignment]
    _LG_StateGraph = None  # type: ignore[assignment]
    _LANGGRAPH_AVAILABLE = False
    _LANGGRAPH_IMPORT_ERROR: str | None = str(_exc)
else:
    _LANGGRAPH_IMPORT_ERROR = None


class LangGraphUnavailable(RuntimeError):
    """Raised when the on-path is requested but ``langgraph`` cannot be imported.

    Callers that must degrade honestly catch this and report ``skipped`` rather
    than fabricating a successful graph run (红线 10).
    """


def langgraph_available() -> bool:
    """Whether ``langgraph`` could be imported in this process."""
    return _LANGGRAPH_AVAILABLE


def langgraph_version() -> str | None:
    """The installed ``langgraph`` version, or ``None`` when unavailable.

    ``None`` means "not measured" (the package is absent) — it is never coerced
    to an empty string or a fabricated version (红线 4).
    """
    if not _LANGGRAPH_AVAILABLE:
        return None
    try:
        import importlib.metadata as _md

        return str(_md.version("langgraph"))
    except Exception:  # noqa: BLE001 — a version read must never gate a run
        return None


# --------------------------------------------------------------------------- #
# Vocabulary                                                                   #
# --------------------------------------------------------------------------- #
# Result "path" — which implementation produced the result.
PATH_INLINE = "inline"
PATH_GRAPH = "graph"
PATH_SKIPPED = "skipped"

# Run statuses (the shared state's lifecycle).
RUNNING = "running"
PAUSED = "paused"
DONE = "done"
DENIED = "denied"
EXPIRED_STATUS = "expired"
CANCELLED_STATUS = "cancelled"
FAILED = "failed"
HALTED = "halted"
SKIPPED = "skipped"

#: Statuses a run will no longer change on its own.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {DONE, DENIED, EXPIRED_STATUS, CANCELLED_STATUS, FAILED, HALTED, SKIPPED}
)

#: Default execution budget when the caller supplies none (红线 18). One declared
#: step may be executed at most once, so ``len(steps)`` is the natural ceiling;
#: :func:`_default_budget` clamps it to a minimum of 1.
DEFAULT_MAX_ITERATIONS = 0  # 0 ⇒ "derive from the step count"


def _default_budget(step_count: int) -> int:
    """Derive a sane execution budget from the number of declared steps.

    At least ``1`` so an empty/one-step run still has a terminating bound.
    """
    return max(1, int(step_count))


# --------------------------------------------------------------------------- #
# Shared state (single source of truth — shared, never copied)                 #
# --------------------------------------------------------------------------- #
def _pending_id(run_id: str, kind: str, cursor: int) -> str:
    """A deterministic pending id (so off/on runs are byte-comparable).

    Determinism is deliberate: the T21 store's own default id is a random UUID,
    which would make two otherwise-identical runs differ. A deterministic id
    keeps :meth:`SkillRunState.to_dict` reusable as the byte-identity oracle.
    """
    return f"pa-{run_id}-{kind}-{cursor}"


def _jsonable(value: Any) -> Any:
    """Coerce ``value`` to something JSON-serialisable (``str`` fallback)."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return str(value)
    return value


@dataclass
class SkillRunState:
    """The run's skill-execution state — the **single source of truth**.

    This object is created by the run's owner (the "主图") and handed to the
    subgraph by reference. The subgraph never deep-copies it; both the inline
    path and the LangGraph path mutate *this same object*, so main-graph and
    subgraph always agree on the run's final state (:func:`reconcile`).

    Attributes:
        run_id: the run this state belongs to.
        tenant_id: the owning tenant (HITL scope). ``None`` means unresolved — a
            pause that needs a tenant then fails closed.
        role: the actor's role, re-checked against RBAC before each step.
        steps: the declared, executable procedure (T03 :class:`SkillStep`).
        cursor: the index of the next step to gate/execute.
        executed: append-only records of steps that actually ran.
        pending_id: the T21 :class:`PendingAction` this run is waiting on, or
            ``None``.
        paused: whether the run is currently waiting on a human.
        resolved: whether a pending action has been resolved for this run.
        status: one of the run statuses above.
        reason: a human-readable explanation for a non-``running`` status.
        audit: the append-only HITL audit trail (pause/resolve decisions).
    """

    run_id: str
    tenant_id: str | None = None
    role: str = "viewer"
    steps: list[SkillStep] = field(default_factory=list)
    cursor: int = 0
    executed: list[dict[str, Any]] = field(default_factory=list)
    pending_id: str | None = None
    paused: bool = False
    resolved: bool = False
    status: str = RUNNING
    reason: str | None = None
    audit: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """The canonical, deterministic projection of this state.

        Doubles as the byte-identity oracle: :func:`_canonical` of two states is
        equal iff they are "the same run outcome". Datetimes/uuids never leak in,
        so the projection is stable across processes.
        """
        return {
            "run_id": self.run_id,
            "tenant_id": self.tenant_id,
            "role": self.role,
            "cursor": self.cursor,
            "status": self.status,
            "reason": self.reason,
            "pending_id": self.pending_id,
            "paused": self.paused,
            "resolved": self.resolved,
            "steps": [
                {
                    "purpose": s.purpose,
                    "tool": s.tool,
                    "input": list(s.input_keys),
                    "output": list(s.output_keys),
                    "validation": s.validation,
                }
                for s in self.steps
            ],
            "executed": [_jsonable(r) for r in self.executed],
            "audit": [_jsonable(a) for a in self.audit],
        }

    def restore(self, snapshot: dict[str, Any]) -> None:
        """Restore every mutable field from a :meth:`to_dict` snapshot.

        Used to guarantee a failing subgraph leaves **no** partial write on the
        shared state (子图抛错 ⇒ 不得污染主图状态).
        """
        self.run_id = snapshot["run_id"]
        self.tenant_id = snapshot["tenant_id"]
        self.role = snapshot["role"]
        self.cursor = snapshot["cursor"]
        self.status = snapshot["status"]
        self.reason = snapshot["reason"]
        self.pending_id = snapshot["pending_id"]
        self.paused = snapshot["paused"]
        self.resolved = snapshot["resolved"]
        self.steps = [
            SkillStep(
                purpose=str(s.get("purpose", "")),
                tool=str(s.get("tool", "")),
                input_keys=[str(x) for x in s.get("input", [])],
                output_keys=[str(x) for x in s.get("output", [])],
                validation=str(s.get("validation", "")),
            )
            for s in snapshot["steps"]
        ]
        self.executed = [dict(r) for r in snapshot["executed"]]
        self.audit = [dict(a) for a in snapshot["audit"]]


def _canonical(state: SkillRunState) -> str:
    """A stable string form of a state (sorted keys, no ASCII escaping)."""
    return json.dumps(state.to_dict(), sort_keys=True, ensure_ascii=False)


@dataclass
class SkillRunResult:
    """The outcome of one :func:`execute_skill_run` call.

    Attributes:
        path: which implementation produced this result — ``inline`` / ``graph``
            / ``skipped``.
        status: the run status reached (mirrors ``state.status`` on success).
        run_id: the run id.
        cursor: the state's cursor after the call.
        executed: the state's executed records after the call.
        pending_id: the pending action the run is waiting on (``None`` otherwise).
        reason: the human-readable reason for a non-``running``/``done`` status.
        final_projection: ``state.to_dict()`` after the call — the shared-state
            truth the caller can compare against its own view.
        degraded: whether this result is an honest degradation (missing
            dependency / no runner), never a fabricated success.
    """

    path: str
    status: str
    run_id: str
    cursor: int
    executed: list[dict[str, Any]] = field(default_factory=list)
    pending_id: str | None = None
    reason: str | None = None
    final_projection: dict[str, Any] = field(default_factory=dict)
    degraded: bool = False


def reconcile(main_state: SkillRunState, result: SkillRunResult) -> bool:
    """Whether ``result`` and ``main_state`` agree on the run's final state.

    The subgraph shares the caller's object, so this is ``True`` by construction;
    it exists as an explicit, assertable consistency check (主图与子图口径一致).
    """
    return result.final_projection == main_state.to_dict()


# --------------------------------------------------------------------------- #
# Procedure helper (T03 — the one loader)                                      #
# --------------------------------------------------------------------------- #
def steps_from_spec(spec: dict[str, Any] | None) -> list[SkillStep]:
    """Load the executable procedure from a skill spec (delegates to T03).

    Re-exported so a caller wires the subgraph to the *same* ``spec["procedure"]``
    semantics as the rest of the platform — the legacy ``spec["steps"]`` is never
    consulted here either.
    """
    return load_procedure(spec)


# --------------------------------------------------------------------------- #
# Core engine — pure helpers shared by BOTH paths (so they cannot drift)        #
# --------------------------------------------------------------------------- #
StepRunner = Callable[[SkillStep, SkillRunState], Any]


def _store_or_default(store: PendingActionStore | None) -> PendingActionStore:
    """The T21 pending store (an injected one wins — used by tests)."""
    return store if store is not None else get_pending_store()


def _not_in_whitelist_reason(tool: str) -> str:
    """Verbatim fail-closed reason for a step tool outside the whitelist."""
    return f"技能声明的工具 '{tool}' 不在平台白名单内，拒绝执行"


def _halt_budget(state: SkillRunState) -> None:
    """Mark a run halted for exceeding its execution budget (红线 18)."""
    state.status = HALTED
    state.reason = (
        f"步骤执行超出预算（已执行 {len(state.executed)} 步），已终止"
        "（红线 18：循环必须有预算与终止条件）"
    )


def _apply_pending(state: SkillRunState, store: PendingActionStore) -> None:
    """Re-read the run's T21 pending action and react (reuse, never re-invent).

    * ``waiting``   — stays ``paused`` (the run must **not** continue).
    * ``resolved``  — clears the pending, sets ``running`` so the current step
      executes.
    * ``expired``   — fail-closed terminal (``EXPIRED_STATUS``; 红线 21).
    * ``cancelled`` — terminal.
    * missing       — fail-closed terminal (a lost pending never silently passes).
    """
    if state.pending_id is None:
        return
    action = store.get(state.tenant_id, state.pending_id)
    if action is None:
        state.status = FAILED
        state.reason = (
            f"pending action {state.pending_id} not found (fail closed, 红线 21)"
        )
        state.paused = False
        return
    if action.status == WAITING:
        # Still waiting — leave the run paused; do not continue.
        state.status = PAUSED
        state.paused = True
        return
    if action.status == RESOLVED:
        state.resolved = True
        state.paused = False
        state.status = RUNNING
        state.audit.append(
            {
                "event": "hitl",
                "phase": "resolved",
                "pending_id": action.pending_id,
                "kind": action.kind,
                "decision": action.resolution,
            }
        )
        state.pending_id = None
        return
    if action.status == EXPIRED:
        state.status = EXPIRED_STATUS
        state.reason = "pending action expired (fail closed, 红线 21)"
        state.paused = False
        return
    if action.status == CANCELLED:
        state.status = CANCELLED_STATUS
        state.reason = "pending action cancelled"
        state.paused = False
        return
    # Any other status fails closed.
    state.status = FAILED
    state.reason = f"pending action unexpected status {action.status!r} (fail closed)"
    state.paused = False


def _decide_step(
    state: SkillRunState,
    step: SkillStep,
    cursor: int,
    store: PendingActionStore,
) -> None:
    """Gate the current step: RBAC → whitelist → T21 policy → maybe pause/deny.

    Never bypasses HITL (红线 21): a whitelist miss, an RBAC denial or a policy
    ``deny`` all set a terminal status with **no** execution. Only ``auto`` leaves
    the run ``running`` so the caller proceeds to execute.
    """
    # 1) Platform whitelist — no tool outside PLATFORM_PLAN_TOOLS is ever driven.
    if step.tool not in PLATFORM_PLAN_TOOLS:
        state.status = DENIED
        state.reason = _not_in_whitelist_reason(step.tool)
        return
    # 2) RBAC — re-checked immediately before the step (gate, never bypassed).
    if not check_tool_permission(state.role, step.tool):
        state.status = DENIED
        state.reason = describe_denial(state.role, step.tool)
        return
    # 3) T21 policy — classify the tool and decide auto / pause / deny.
    plan = hitl_policy.plan_for_tool(
        step.tool,
        run_id=state.run_id,
        tenant_id=state.tenant_id,
        pending_id=_pending_id(state.run_id, "x", cursor),
        payload={"cursor": cursor, "tool": step.tool, "purpose": step.purpose},
    )
    decision = plan["decision"]
    action = decision.get("action")
    kind = decision.get("kind")

    if action == ACTION_DENY:
        state.status = DENIED
        state.reason = str(decision.get("reason") or "denied by HITL policy")
        return

    if action in (ACTION_PENDING_APPROVAL, ACTION_CONFIRM_PERMISSION):
        if not state.tenant_id:
            state.status = DENIED
            state.reason = "tenant required to pause for a human (fail closed)"
            return
        pid = _pending_id(state.run_id, str(kind), cursor)
        payload = plan["pending"].payload if plan.get("pending") is not None else {}
        record = store.create(
            state.tenant_id,
            run_id=state.run_id,
            kind=str(kind),
            payload=payload,
            pending_id=pid,
        )
        state.pending_id = record.pending_id
        state.paused = True
        state.status = PAUSED
        state.audit.append(
            {
                "event": "hitl",
                "phase": "paused",
                "pending_id": record.pending_id,
                "kind": record.kind,
                "cursor": cursor,
                "tool": step.tool,
            }
        )
        return

    # ACTION_AUTO — nothing to do; the caller proceeds to execute.
    if action != ACTION_AUTO:  # defensive: an unknown action fails closed
        state.status = DENIED
        state.reason = f"unknown HITL action {action!r} (fail closed)"


async def _execute_current(state: SkillRunState, runner: StepRunner | None) -> None:
    """Execute the step at ``cursor`` via ``runner`` and record the outcome.

    A missing runner is an honest ``unavailable`` degradation (红线 10) that
    stops the run — never a fabricated success. The RBAC/whitelist gate has
    already run in :func:`_decide_step`.
    """
    cursor = state.cursor
    if cursor >= len(state.steps):
        return
    step = state.steps[cursor]
    if runner is None:
        state.executed.append(
            {
                "index": cursor,
                "tool": step.tool,
                "status": "unavailable",
                "detail": "no step runner configured (honest degradation, 红线 10)",
            }
        )
        state.status = FAILED
        state.reason = "no step runner configured (honest degradation, 红线 10)"
        return
    result = runner(step, state)
    if inspect.isawaitable(result):
        result = await result
    state.executed.append(
        {"index": cursor, "tool": step.tool, "status": "ok", "detail": _jsonable(result)}
    )


def _finalize(state: SkillRunState) -> None:
    """Set the terminal ``done`` status when the run finished all steps."""
    if state.status != RUNNING:
        return
    if state.cursor >= len(state.steps):
        state.status = DONE
        if not state.steps:
            state.reason = "技能未声明可执行步骤（procedure 为空）"


# --------------------------------------------------------------------------- #
# Inline reference path (the "打开前" behaviour — default, flag OFF)            #
# --------------------------------------------------------------------------- #
async def _run_inline(
    state: SkillRunState,
    *,
    runner: StepRunner | None,
    max_iterations: int,
    store: PendingActionStore,
) -> SkillRunResult:
    """Drive the whole procedure inline (no LangGraph).

    Semantically identical to the graph path (both call the very same helpers), so
    the two are interchangeable — the flag only selects *which* drives the same
    contract. Honours the execution budget and the T21 pause mechanism exactly
    like the graph path.
    """
    budget = max_iterations if max_iterations > 0 else _default_budget(len(state.steps))
    loop_guard = 4 * budget + 4
    guard = 0

    while True:
        guard += 1
        if guard > loop_guard:
            _halt_budget(state)
            break

        if state.pending_id is not None:
            _apply_pending(state, store)
            if state.status != RUNNING or state.paused:
                break
            # resolved → fall through and execute the current cursor step
        else:
            if state.cursor >= len(state.steps):
                break
            _decide_step(state, state.steps[state.cursor], state.cursor, store)
            if state.status != RUNNING:
                break

        if len(state.executed) >= budget:
            _halt_budget(state)
            break
        await _execute_current(state, runner)
        if state.status != RUNNING:
            break
        state.cursor += 1

    _finalize(state)
    return SkillRunResult(
        path=PATH_INLINE,
        status=state.status,
        run_id=state.run_id,
        cursor=state.cursor,
        executed=list(state.executed),
        pending_id=state.pending_id,
        reason=state.reason,
        final_projection=state.to_dict(),
    )


# --------------------------------------------------------------------------- #
# LangGraph subgraph path (flag ON)                                            #
# --------------------------------------------------------------------------- #
class _GraphState(TypedDict, total=False):
    """The LangGraph channel state: the shared object + an iteration counter.

    ``shared`` carries the **same object reference** the caller handed in; nodes
    mutate it in place and only ever overwrite the ``iter`` counter. Routing
    reads the authoritative object off :class:`SkillSubgraph` (``self.state``), so
    the truth is never a channel copy.
    """

    shared: SkillRunState
    iter: int


class SkillSubgraph:
    """A compiled LangGraph subgraph that drives one skill procedure.

    The subgraph **shares** the caller's :class:`SkillRunState` (``self.state is
    main_state``); it never holds an independent copy. It reuses the T21 pending
    store for every pause, and a run that raises leaves the shared state restored
    to its pre-run snapshot.
    """

    def __init__(
        self,
        state: SkillRunState,
        *,
        runner: StepRunner | None = None,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        store: PendingActionStore | None = None,
    ) -> None:
        self._state = state
        self._runner = runner
        self._max_iterations = int(max_iterations)
        self._store = store
        self._iterations = 0

    @property
    def state(self) -> SkillRunState:
        """The shared run state (identical object handed in by the caller)."""
        return self._state

    # -- graph nodes (bound methods — LangGraph calls them with only the state) #
    async def _gate_node(self, gstate: _GraphState) -> dict[str, Any]:
        """Graph node — gate/resume the run's current step (see :func:`_decide_step`)."""
        state = self._state
        store = _store_or_default(self._store)
        if state.pending_id is not None:
            _apply_pending(state, store)
        elif state.cursor < len(state.steps) and state.status == RUNNING:
            _decide_step(state, state.steps[state.cursor], state.cursor, store)
        return {"iter": int(gstate.get("iter", 0)) + 1}

    async def _execute_node(self, gstate: _GraphState) -> dict[str, Any]:
        """Graph node — execute the current step under the execution budget."""
        it = int(gstate.get("iter", 0)) + 1
        state = self._state
        budget = (
            self._max_iterations
            if self._max_iterations > 0
            else _default_budget(len(state.steps))
        )
        if len(state.executed) >= budget:
            _halt_budget(state)
            return {"iter": it}
        await _execute_current(state, self._runner)
        if state.status == RUNNING:
            state.cursor += 1
        return {"iter": it}

    async def _finalize_node(self, gstate: _GraphState) -> dict[str, Any]:
        """Graph node — finalize the run status."""
        _finalize(self._state)
        return {"iter": int(gstate.get("iter", 0)) + 1}

    # -- routers (read the authoritative shared object, never a channel copy) #
    def _route_after_gate(self, gstate: _GraphState) -> str:
        """Route after the gate: execute the step, or finalize (halt/paused/end)."""
        state = self._state
        if state.status != RUNNING or state.paused:
            return "finalize"
        if state.cursor >= len(state.steps):
            return "finalize"
        return "execute"

    def _route_after_execute(self, gstate: _GraphState) -> str:
        """Route after execute: the next step's gate, or finalize."""
        state = self._state
        if state.status != RUNNING or state.paused:
            return "finalize"
        if state.cursor >= len(state.steps):
            return "finalize"
        return "gate"

    # -- graph construction -------------------------------------------------- #
    def _build_graph(self) -> Any:
        """Compile the subgraph (raises :class:`LangGraphUnavailable` when absent)."""
        if not _LANGGRAPH_AVAILABLE:
            raise LangGraphUnavailable(
                f"langgraph 不可用，Skill 子图无法执行（诚实降级，红线 10）：{_LANGGRAPH_IMPORT_ERROR}"
            )
        builder = _LG_StateGraph(_GraphState)
        builder.add_node("gate", self._gate_node)
        builder.add_node("execute", self._execute_node)
        builder.add_node("finalize", self._finalize_node)
        builder.set_entry_point("gate")
        builder.add_conditional_edges(
            "gate",
            self._route_after_gate,
            {"execute": "execute", "finalize": "finalize"},
        )
        builder.add_conditional_edges(
            "execute",
            self._route_after_execute,
            {"gate": "gate", "finalize": "finalize"},
        )
        builder.add_edge("finalize", _LG_END)
        return builder.compile()

    # -- run ----------------------------------------------------------------- #
    async def run(self) -> SkillRunResult:
        """Run the subgraph, sharing (and, on success, mutating) the shared state.

        Returns a :class:`SkillRunResult` whose ``path`` is ``graph`` on a real
        run or ``skipped`` when langgraph is unavailable. On any error the shared
        state is restored to its pre-run snapshot and the result reports
        ``failed`` (子图抛错 ⇒ 不得污染主图状态).
        """
        global _GRAPH_RUN_COUNT
        snapshot = self._state.to_dict()
        _GRAPH_RUN_COUNT += 1

        if not _LANGGRAPH_AVAILABLE:
            reason = (
                "langgraph 不可用，Skill 子图跳过（诚实降级，未假装执行，红线 10）："
                f"{_LANGGRAPH_IMPORT_ERROR}"
            )
            logger.warning("skill subgraph skipped: %s", reason)
            return SkillRunResult(
                path=PATH_SKIPPED,
                status=SKIPPED,
                run_id=self._state.run_id,
                cursor=self._state.cursor,
                executed=list(self._state.executed),
                pending_id=self._state.pending_id,
                reason=reason,
                final_projection=self._state.to_dict(),
                degraded=True,
            )

        try:
            graph = self._build_graph()
            initial = {"shared": self._state, "iter": 0}
            final = await graph.ainvoke(initial)
            self._iterations = int(final.get("iter", 0)) if isinstance(final, dict) else 0
            if self._state.status == RUNNING:
                _finalize(self._state)
        except LangGraphUnavailable as exc:
            self._state.restore(snapshot)
            return SkillRunResult(
                path=PATH_SKIPPED,
                status=SKIPPED,
                run_id=self._state.run_id,
                cursor=self._state.cursor,
                executed=list(self._state.executed),
                pending_id=self._state.pending_id,
                reason=str(exc),
                final_projection=self._state.to_dict(),
                degraded=True,
            )
        except Exception as exc:  # noqa: BLE001 — a subgraph fault must not pollute the run
            self._state.restore(snapshot)
            logger.warning("skill subgraph failed (state restored): %s", exc)
            return SkillRunResult(
                path=PATH_GRAPH,
                status=FAILED,
                run_id=self._state.run_id,
                cursor=self._state.cursor,
                executed=list(self._state.executed),
                pending_id=self._state.pending_id,
                reason=f"{type(exc).__name__}: {exc}",
                final_projection=self._state.to_dict(),
                degraded=False,
            )

        return SkillRunResult(
            path=PATH_GRAPH,
            status=self._state.status,
            run_id=self._state.run_id,
            cursor=self._state.cursor,
            executed=list(self._state.executed),
            pending_id=self._state.pending_id,
            reason=self._state.reason,
            final_projection=self._state.to_dict(),
        )


# --------------------------------------------------------------------------- #
# Graph-run counter (test observability — proves the off-path never runs it)    #
# --------------------------------------------------------------------------- #
_GRAPH_RUN_COUNT = 0


def graph_run_count() -> int:
    """How many times the graph path has been entered in this process."""
    return _GRAPH_RUN_COUNT


def reset_graph_run_count() -> None:
    """Reset the graph-run counter (test helper)."""
    global _GRAPH_RUN_COUNT
    _GRAPH_RUN_COUNT = 0


# --------------------------------------------------------------------------- #
# Entry points                                                                 #
# --------------------------------------------------------------------------- #
async def run_skill_subgraph(
    state: SkillRunState,
    *,
    runner: StepRunner | None = None,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    store: PendingActionStore | None = None,
) -> SkillRunResult:
    """Run the LangGraph skill subgraph over the shared ``state``.

    Bypasses the feature flag (this is the explicit "force the graph" entry).
    """
    sub = SkillSubgraph(
        state, runner=runner, max_iterations=max_iterations, store=store
    )
    return await sub.run()


async def execute_skill_run(
    state: SkillRunState,
    *,
    runner: StepRunner | None = None,
    use_graph: bool | None = None,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    store: PendingActionStore | None = None,
) -> SkillRunResult:
    """Dispatch a skill run to the inline or the LangGraph path.

    Args:
        state: the shared run state (mutated in place; never copied).
        runner: how a step is executed (``None`` ⇒ honest ``unavailable``).
        use_graph: force a path. ``None`` (default) derives it from
            :func:`skill_subgraph_enabled` — the feature flag. ``False`` selects
            the inline reference path (the "打开前" behaviour); ``True`` selects
            the LangGraph subgraph.
        max_iterations: the execution budget (红线 18); ``0`` derives it.
        store: the T21 pending store (defaults to the process store).

    Returns:
        A :class:`SkillRunResult`. With the flag off this is always
        ``path="inline"`` and the LangGraph machinery is not entered — the
        zero-regression guarantee.
    """
    resolved_store = _store_or_default(store)
    if use_graph is None:
        use_graph = skill_subgraph_enabled()
    if use_graph:
        return await run_skill_subgraph(
            state, runner=runner, max_iterations=max_iterations, store=resolved_store
        )
    return await _run_inline(
        state,
        runner=runner,
        max_iterations=max_iterations,
        store=resolved_store,
    )


# --------------------------------------------------------------------------- #
# T28 hook — run ONE agent-loop plan step through the subgraph (ADDITIVE ONLY)  #
#                                                                               #
# The Agent main loop (forgeflow/agent/loop.py) executes its steps through an    #
# injectable hook. This is the seam: it hands a single plan step to the *same*   #
# dispatcher the rest of the platform uses (:func:`execute_skill_run`), so the   #
# loop rides the existing gate + HITL + trace machinery instead of a private     #
# copy. Nothing below changes any existing behaviour — the off-path of           #
# :func:`execute_skill_run` is untouched and the flag still defaults to OFF.     #
# --------------------------------------------------------------------------- #
#: Subgraph terminal status → the T28 hook vocabulary the loop understands.
#: Only ``done`` / ``paused`` continue a loop; every other status is terminal and
#: fail-closed (a ``denied`` step must never be retried as a generic error).
_HOOK_STATUS: dict[str, str] = {
    DONE: "done",
    PAUSED: "paused",
    DENIED: "denied",
    EXPIRED_STATUS: "denied",
    CANCELLED_STATUS: "denied",
    FAILED: "failed",
    HALTED: "failed",
    SKIPPED: "skipped",
}


@dataclass
class PlanStepHookResult:
    """Outcome of running one agent-loop plan step through the subgraph.

    The three load-bearing fields — ``status`` / ``reason`` / ``pending_id`` —
    are exactly what :class:`forgeflow.agent.loop.AgentLoop`'s default step runner
    reads (via ``getattr``), so the agent package and this module agree by
    contract **without either importing the other** (no import cycle).

    ``status`` is one of ``done`` (matched) / ``paused`` (waiting on a human) /
    ``denied`` / ``failed`` / ``skipped``.
    """

    status: str
    reason: str = ""
    pending_id: str | None = None
    path: str = PATH_INLINE
    executed: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "pending_id": self.pending_id,
            "path": self.path,
            "executed": [_jsonable(r) for r in self.executed],
        }


def _plan_step_tool_runner(plan_step: Any, *, user_id: str, intent: str) -> StepRunner:
    """A :data:`StepRunner` that executes the *plan step's* declared tool.

    A subgraph :class:`SkillStep` declares a tool but not its arguments, so the
    plan step's ``args`` are bound here (the plan is the run's own declaration —
    this is not a new tool-selection path). Execution still goes through the
    platform's single honest choke point
    (:class:`~forgeflow.runtime.tool_executor.ToolExecutor`), exactly as every
    other runtime step does; the RBAC / whitelist gate has already run inside the
    subgraph before this is ever called (红线 5 / 21).
    """

    async def _runner(skill_step: SkillStep, state: SkillRunState) -> dict[str, Any]:
        from forgeflow.runtime.tool_executor import ToolCallContext, ToolExecutor

        invocation = await ToolExecutor().execute(
            skill_step.tool,
            ctx=ToolCallContext(
                run_id=state.run_id,
                step_id=f"{state.run_id}:0:0",
                tenant_id=state.tenant_id,
                user_id=user_id,
                role=state.role,
                intent=intent,
                args=dict(getattr(plan_step, "args", {}) or {}),
            ),
            policy_decision="not_evaluated",
        )
        return invocation.to_dict()

    return _runner


def agent_loop_step_hook(
    *,
    run_id: str,
    tenant_id: str | None = None,
    role: str = "viewer",
    user_id: str = "anonymous",
    intent: str = "",
    store: PendingActionStore | None = None,
    use_graph: bool | None = None,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
) -> Callable[[Any, Any], Awaitable[PlanStepHookResult]]:
    """Build the async ``(plan_step, loop_state) -> PlanStepHookResult`` hook.

    Pass the returned callable to
    :class:`forgeflow.agent.loop.AgentLoop` as ``step_hook=...`` and each plan
    step is dispatched through the skill subgraph
    (:func:`execute_skill_run`) — one ``SkillRunState`` per step — so the loop
    inherits the subgraph's gate, HITL pause and honest degradation.

    Args:
        run_id / tenant_id / role / user_id / intent: the run identity the
            subgraph gates against.
        store: the T21 pending store (default: the process store).
        use_graph: force the path; ``None`` derives it from
            :func:`skill_subgraph_enabled` (the ``FORGEFLOW_SKILL_SUBGRAPH`` flag,
            default OFF) — so with the flag off the hook still runs, but through
            the inline reference path.
        max_iterations: the subgraph execution budget (红线 18); ``0`` derives it.

    Returns:
        An awaitable hook. It never raises for a step outcome — a failure is an
        honest ``status="failed"`` (红线 10), never a fabricated success.
    """

    async def _hook(plan_step: Any, loop_state: Any = None) -> PlanStepHookResult:
        skill_state = SkillRunState(
            run_id=run_id,
            tenant_id=tenant_id,
            role=role,
            steps=[
                SkillStep(
                    purpose=str(getattr(plan_step, "purpose", "") or ""),
                    tool=str(getattr(plan_step, "tool", "") or ""),
                )
            ],
        )
        runner = _plan_step_tool_runner(plan_step, user_id=user_id, intent=intent)
        result = await execute_skill_run(
            skill_state,
            runner=runner,
            use_graph=use_graph,
            max_iterations=max_iterations,
            store=store,
        )
        return PlanStepHookResult(
            status=_HOOK_STATUS.get(result.status, "failed"),
            reason=result.reason or "",
            pending_id=result.pending_id,
            path=result.path,
            executed=list(result.executed),
        )

    return _hook
