"""INC46 T28 — the **Agent main loop** (Plan → Act → Observe → Replan).

What this module is
-------------------
T12 made a selected skill's procedure runnable through a LangGraph subgraph. T28
adds the missing *intelligence layer* on top of it: a **bounded** dynamic loop
that plans, acts, observes whether reality matched the plan, and replans when it
did not — terminating deterministically, with a full trace.

It is deliberately self-contained and **feature-flagged** (``agent.loop``, default
OFF). It adds **no new API surface**; the loop reuses the platform's existing
execution entry (``forgeflow.runtime.tool_executor.ToolExecutor`` — the single
honest choke point) and the existing HITL primitive
(``forgeflow.hitl.pending`` — T21). With the flag off a call is an honest no-op
(:data:`DISABLED`), so today's behaviour is untouched (零回归).

The four phases
---------------
* **Plan** — a :class:`~forgeflow.agent.plan.Plan` (``plan_id`` / steps /
  assumptions / success criteria / budget). Built by an injected ``planner`` or
  supplied directly.
* **Act** — one step at a time, gated: a step whose tool is outside the caller's
  **eligibility set** is refused fail-closed and never runs (红线 5).
* **Observe** — the runner returns a typed :class:`Observation`; the loop compares
  it to the step's ``expected``.
* **Replan** — on a mismatch (tool error / validation failure / ambiguity /
  permission denial) the loop replans. A replan **can never widen permission**:
  every replanned step is re-checked against the eligibility set and a plan that
  reaches outside it is refused (fail-closed).

Hard guarantees
---------------
* **红线 18 (budget + termination).** Every loop has an explicit
  :class:`~forgeflow.agent.budget.Budget` (defaults 25 steps / 3 replans / 300 s /
  200k tokens). ANY ceiling being exhausted stops the loop with
  ``status="budget_exceeded"`` — which is **not success** — and the result carries
  the completed / unfinished lists. A cycle detector
  (:class:`~forgeflow.agent.loop_guard.LoopGuard`) terminates a same-action,
  no-progress spin with ``status="loop_detected"``.
* **红线 21 (never bypass HITL).** Plan mode for a risky (WRITE / EXTERNAL /
  DANGEROUS) plan pauses through T21's ``PendingActionStore`` and only proceeds
  once the action is resolved; a ``clarify`` step asks a human through the same
  store. There is no second pause mechanism.
* **红线 5 (tenant / RBAC fail-closed).** An unresolved tenant cannot pause
  (denied); a tool outside the eligibility set is refused; a plan-mode approval
  requires a tenant.

Trace
-----
Every iteration writes one ``run_steps`` row (through the existing trace substrate
— **no migration**) carrying the ``plan_id``, the iteration index and the
observation kind, so a run's trace is groupable by plan and the "2 次重规划"
trajectory is a set of *real* plan ids.

Feature flag
------------
:func:`agent_loop_enabled` reads ``FORGEFLOW_AGENT_LOOP`` (and, as aliases, the
literal ``agent.loop`` / ``AGENT_LOOP``) at call time; default OFF. It is a
module-local reader (same discipline as T12's ``skill_subgraph_enabled``) so no
shared config file is touched.
"""

from __future__ import annotations

import inspect
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from forgeflow.agent.budget import (
    Budget,
    BudgetBreach,
    BudgetTracker,
    check_budget,
)
from forgeflow.agent.loop_guard import (
    DEFAULT_REPEAT_THRESHOLD,
    LoopGuard,
    LoopSignal,
)
from forgeflow.agent.plan import (
    STEP_CLARIFY,
    STEP_FIX,
    STEP_REPORT,
    STEP_TOOL,
    Plan,
    PlanStep,
    has_risky_steps,
    new_plan,
)

logger = logging.getLogger(__name__)

__all__ = [
    # feature flag
    "FEATURE_FLAG_ENV",
    "FEATURE_FLAG_ALIASES",
    "agent_loop_enabled",
    # paths / statuses
    "PATH_LOOP",
    "PATH_DISABLED",
    "DONE",
    "BUDGET_EXCEEDED",
    "LOOP_DETECTED",
    "AWAITING_APPROVAL",
    "DENIED",
    "FAILED",
    "DISABLED",
    "TERMINAL_STATUSES",
    # observation kinds
    "OBS_OK",
    "OBS_TOOL_ERROR",
    "OBS_VALIDATION_FAILED",
    "OBS_AMBIGUITY",
    "OBS_PERMISSION_DENIED",
    "OBS_AWAITING_APPROVAL",
    "REPLAN_TRIGGERS",
    # objects
    "RunContext",
    "LoopState",
    "Observation",
    "LoopResult",
    "AgentLoop",
    # helpers
    "eligible_tools",
    "run_agent_loop",
]

# --------------------------------------------------------------------------- #
# Feature flag (module-local by design — see module docstring)                 #
# --------------------------------------------------------------------------- #
#: Primary environment variable that opts a process into the Agent main loop.
FEATURE_FLAG_ENV = "FORGEFLOW_AGENT_LOOP"
#: Accepted aliases (the task book names the flag ``agent.loop``).
FEATURE_FLAG_ALIASES: tuple[str, ...] = ("FORGEFLOW_AGENT_LOOP", "AGENT_LOOP", "agent.loop")
_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})


def agent_loop_enabled() -> bool:
    """Whether the Agent main loop is enabled in this process.

    Reads the env **at call time** (never cached) and returns ``True`` only for
    ``{"1","true","yes","on"}`` (case-insensitive). Default ``False`` — the loop
    is opt-in and OFF unless explicitly enabled. Checks
    :data:`FEATURE_FLAG_ALIASES` in order; the first *set* variable wins.
    """
    for name in FEATURE_FLAG_ALIASES:
        raw = os.environ.get(name)
        if raw is None:
            continue
        return str(raw).strip().lower() in _TRUTHY
    return False


# --------------------------------------------------------------------------- #
# Vocabulary                                                                   #
# --------------------------------------------------------------------------- #
#: Result "path" — which implementation produced the result.
PATH_LOOP = "loop"
PATH_DISABLED = "disabled"

# Run statuses.
DONE = "done"
BUDGET_EXCEEDED = "budget_exceeded"
LOOP_DETECTED = "loop_detected"
AWAITING_APPROVAL = "awaiting_approval"
DENIED = "denied"
FAILED = "failed"
DISABLED = "disabled"

#: Statuses that mean "the loop will not change on its own".
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {DONE, BUDGET_EXCEEDED, LOOP_DETECTED, DENIED, FAILED, DISABLED}
)

# Observation kinds (what Act produced).
OBS_OK = "ok"
OBS_TOOL_ERROR = "tool_error"
OBS_VALIDATION_FAILED = "validation_failed"
OBS_AMBIGUITY = "ambiguity"
OBS_PERMISSION_DENIED = "permission_denied"
OBS_AWAITING_APPROVAL = "awaiting_approval"

#: Observations that trigger a replan (task book TABLE 36).
REPLAN_TRIGGERS: frozenset[str] = frozenset(
    {OBS_TOOL_ERROR, OBS_VALIDATION_FAILED, OBS_AMBIGUITY, OBS_PERMISSION_DENIED}
)

#: Human-readable label per observation kind (used in reasons / traces).
_OBS_LABEL: dict[str, str] = {
    OBS_OK: "成功",
    OBS_TOOL_ERROR: "工具错误",
    OBS_VALIDATION_FAILED: "验证失败",
    OBS_AMBIGUITY: "定位歧义",
    OBS_PERMISSION_DENIED: "权限拒绝",
    OBS_AWAITING_APPROVAL: "等待人工",
}


# --------------------------------------------------------------------------- #
# Eligibility (红线 5 — fail-closed)                                           #
# --------------------------------------------------------------------------- #
def eligible_tools(role: str) -> frozenset[str]:
    """The set of platform tools ``role`` may drive inside a run (fail-closed).

    Intersection of the platform whitelist (``gate.PLATFORM_PLAN_TOOLS``) and the
    role's RBAC grants (``gate.check_tool_permission``). A tool outside this set
    is refused by the loop and by the executor alike — a replan can never widen
    it (红线 5). Imported lazily so ``forgeflow.agent`` never forms an import
    cycle with ``forgeflow.runtime``.
    """
    from forgeflow.runtime.gate import PLATFORM_PLAN_TOOLS, check_tool_permission

    return frozenset(tool for tool in PLATFORM_PLAN_TOOLS if check_tool_permission(role, tool))


# --------------------------------------------------------------------------- #
# Value objects                                                                #
# --------------------------------------------------------------------------- #
@dataclass
class RunContext:
    """Identity / tenant scope for one Agent Loop run (mirrors the runtime ctx)."""

    run_id: str
    tenant_id: str | None = None
    role: str = "viewer"
    user_id: str = "anonymous"
    intent: str = ""


@dataclass
class LoopState:
    """The mutable loop state handed to a runner (progress bookkeeping)."""

    run_id: str
    plan_id: str
    iteration: int = 0
    replans: int = 0
    cursor: int = 0


@dataclass
class Observation:
    """What one Act produced (the Observe phase's input).

    ``kind`` is one of the ``OBS_*`` constants. ``progress`` says whether the
    action changed any state — the loop guard uses it to tell a real retry apart
    from a spin. ``pending_id`` is set when the step paused for a human.
    """

    kind: str = OBS_OK
    step_id: str = ""
    tool: str = ""
    detail: str = ""
    progress: bool = True
    tokens: int = 0
    latency_ms: float | None = None
    pending_id: str | None = None
    raw: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "step_id": self.step_id,
            "tool": self.tool,
            "detail": self.detail,
            "progress": self.progress,
            "tokens": self.tokens,
            "latency_ms": self.latency_ms,
            "pending_id": self.pending_id,
        }


@dataclass
class LoopResult:
    """The outcome of one :func:`AgentLoop.run` (or :func:`run_agent_loop`)."""

    status: str
    path: str = PATH_LOOP
    plan_id: str | None = None
    plan_ids: list[str] = field(default_factory=list)
    plans: list[dict[str, Any]] = field(default_factory=list)
    completed: list[dict[str, Any]] = field(default_factory=list)
    unfinished: list[dict[str, Any]] = field(default_factory=list)
    replans: int = 0
    iterations: int = 0
    trace: list[dict[str, Any]] = field(default_factory=list)
    reason: str | None = None
    pending_id: str | None = None
    budget: dict[str, Any] = field(default_factory=dict)
    loop_signal: dict[str, Any] | None = None
    degraded: bool = False

    @property
    def success(self) -> bool:
        """Whether the loop finished its plan. **Exhaustion is never success.**"""
        return self.status == DONE

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "path": self.path,
            "success": self.success,
            "plan_id": self.plan_id,
            "plan_ids": list(self.plan_ids),
            "plans": list(self.plans),
            "completed": list(self.completed),
            "unfinished": list(self.unfinished),
            "replans": self.replans,
            "iterations": self.iterations,
            "trace": list(self.trace),
            "reason": self.reason,
            "pending_id": self.pending_id,
            "budget": dict(self.budget),
            "loop_signal": self.loop_signal,
            "degraded": self.degraded,
        }


# Type aliases (documentation).
Planner = Callable[[str], Awaitable[Plan]]
Replanner = Callable[[Plan, Observation], Awaitable["Plan | None"]]
StepRunner = Callable[[PlanStep, LoopState], Awaitable[Observation]]
TraceWriter = Callable[[dict[str, Any]], Awaitable[None]]


# --------------------------------------------------------------------------- #
# Default (deterministic) collaborators                                        #
# --------------------------------------------------------------------------- #
async def _default_planner(goal: str) -> Plan:
    """A minimal, always-eligible plan: a single deliverable step.

    Real callers inject a planner (the LLM planner / a skill's declared
    procedure); this default keeps the loop usable stand-alone — and never
    invents a tool outside the whitelist.
    """
    return new_plan(
        [PlanStep(tool="report.render", step_type=STEP_REPORT, purpose=goal or "完成用户目标")],
        assumptions=["目标可直接收敛为一份可交付结果"],
        success_criteria=["产出可交付结果"],
        reason="默认单步计划",
    )


async def _default_replanner(plan: Plan, obs: Observation) -> "Plan | None":
    """The conservative built-in replanner (spec-aligned, fail-closed).

    * ambiguity ⇒ a **clarify** plan (ask a human, via T21);
    * validation failure / tool error ⇒ a **fix** plan (retry the same declared
      tool — never a different, un-vetted tool);
    * permission denial ⇒ ``None`` (never replan *around* a denial).
    """
    if obs.kind == OBS_PERMISSION_DENIED:
        return None
    if obs.kind == OBS_AMBIGUITY:
        return new_plan(
            [PlanStep(step_type=STEP_CLARIFY, purpose=obs.detail or "请澄清任务目标")],
            budget=plan.budget,
            assumptions=list(plan.assumptions),
            success_criteria=list(plan.success_criteria),
            reason="定位歧义 ⇒ 重规划为澄清步骤",
        )
    if obs.kind in (OBS_VALIDATION_FAILED, OBS_TOOL_ERROR):
        prev = next((s for s in plan.steps if s.step_id == obs.step_id), None)
        tool = (prev.tool if prev is not None else "") or ""
        return new_plan(
            [
                PlanStep(
                    tool=tool,
                    step_type=STEP_FIX,
                    args=dict(prev.args) if prev is not None else {},
                    purpose=f"修复上一步失败（{_OBS_LABEL.get(obs.kind, obs.kind)}）",
                    expected="修复后观察与预期一致",
                )
            ],
            budget=plan.budget,
            assumptions=list(plan.assumptions),
            success_criteria=list(plan.success_criteria),
            reason=f"观察与预期不符（{obs.kind}）⇒ 重规划到修复",
        )
    return None


def _default_step_runner(ctx: RunContext, *, step_hook: Any | None = None) -> StepRunner:
    """The production step runner: route through the platform's honest entry.

    Uses the T28 skill-subgraph hook when supplied (so a skill step is gated +
    executed by the subgraph), otherwise :class:`ToolExecutor` directly — both
    are the *existing* execution entry, never a new one. Maps the invocation's
    honest status onto an :class:`Observation`.
    """

    async def _runner(step: PlanStep, state: LoopState) -> Observation:
        from forgeflow.runtime.tool_executor import ToolCallContext, ToolExecutor

        if step_hook is not None:
            result = await step_hook(step, state)
            status = str(getattr(result, "status", "") or "")
            detail = str(getattr(result, "reason", "") or status)
            if status == "done":
                return Observation(kind=OBS_OK, step_id=step.step_id, tool=step.tool, detail=detail)
            if status == "paused":
                return Observation(
                    kind=OBS_AWAITING_APPROVAL,
                    step_id=step.step_id,
                    tool=step.tool,
                    detail=detail,
                    pending_id=getattr(result, "pending_id", None),
                )
            if status == "denied":
                # The subgraph failed the step closed (RBAC / whitelist) — surface
                # it as a permission denial, never as a generic retryable error.
                return Observation(
                    kind=OBS_PERMISSION_DENIED, step_id=step.step_id, tool=step.tool, detail=detail
                )
            return Observation(kind=OBS_TOOL_ERROR, step_id=step.step_id, tool=step.tool, detail=detail)

        invocation = await ToolExecutor().execute(
            step.tool,
            ctx=ToolCallContext(
                run_id=ctx.run_id,
                step_id=step.step_id or f"{ctx.run_id}:0:0",
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                role=ctx.role,
                intent=ctx.intent,
                args=dict(step.args or {}),
            ),
            policy_decision="not_evaluated",
        )
        if invocation.status == "ok":
            return Observation(
                kind=OBS_OK,
                step_id=step.step_id,
                tool=step.tool,
                detail=invocation.summary,
                progress=True,
                latency_ms=invocation.latency_ms,
                raw=invocation.payload,
            )
        if invocation.status == "awaiting_approval":
            return Observation(
                kind=OBS_AWAITING_APPROVAL,
                step_id=step.step_id,
                tool=step.tool,
                detail=invocation.summary,
                pending_id=invocation.approval_id,
            )
        return Observation(
            kind=OBS_TOOL_ERROR,
            step_id=step.step_id,
            tool=step.tool,
            detail=invocation.error or invocation.summary,
            progress=False,
            latency_ms=invocation.latency_ms,
        )

    return _runner


async def _default_trace_writer(row: dict[str, Any]) -> None:
    """Persist one iteration into ``run_steps`` (additive; no migration).

    Reuses the existing T01 substrate: the ``invocation`` dict — which carries
    ``plan_id`` / ``iteration`` / ``observation_kind`` — is stored whole on the
    row's JSONB ``output`` column, so no schema change is needed. Best-effort, as
    always: a trace write never gates a run.
    """
    from forgeflow.runtime import trace_store

    await trace_store.persist_invocation(
        row["invocation"],
        args=row.get("args"),
        run_id=row["run_id"],
        tenant_id=row.get("tenant_id"),
        step_id=row.get("step_id"),
    )


# --------------------------------------------------------------------------- #
# The loop                                                                     #
# --------------------------------------------------------------------------- #
class AgentLoop:
    """A bounded Plan–Act–Observe–Replan loop over one goal.

    Args:
        goal: the user goal (used by the default planner / reasons).
        ctx: the run identity / tenant scope.
        plan: a pre-built plan (otherwise ``planner`` builds one).
        planner: ``async (goal) -> Plan`` (default: a single deliverable step).
        replanner: ``async (plan, observation) -> Plan | None`` (default: the
            conservative built-in). ``None`` ⇒ cannot replan around a denial.
        runner: how one step is executed (default: the platform execution entry).
        budget: the ceilings (default: :meth:`Budget.from_settings`).
        eligibility: the tool set the run may drive (default:
            :func:`eligible_tools` for ``ctx.role``).
        store: the T21 pending store (default: the process store).
        trace_writer: how one iteration is persisted (default: ``run_steps``).
        plan_mode: whether a risky plan must be approved via T21 before running.
        clarify_answer: optional ``(step, plan) -> str`` provider; when it returns
            a non-empty answer a ``clarify`` step resolves immediately instead of
            pausing.
        repeat_threshold: loop-guard threshold (default 3).
        clock: injectable monotonic clock (tests).
        use_loop: force the path; ``None`` ⇒ derive from :func:`agent_loop_enabled`.
    """

    def __init__(
        self,
        goal: str,
        *,
        ctx: RunContext,
        plan: Plan | None = None,
        planner: Planner | None = None,
        replanner: Replanner | None = None,
        runner: StepRunner | None = None,
        step_hook: Any | None = None,
        budget: Budget | None = None,
        eligibility: frozenset[str] | None = None,
        store: Any | None = None,
        trace_writer: TraceWriter | None = None,
        plan_mode: bool = False,
        clarify_answer: Callable[[PlanStep, Plan], Any] | None = None,
        repeat_threshold: int = DEFAULT_REPEAT_THRESHOLD,
        clock: Any | None = None,
        use_loop: bool | None = None,
    ) -> None:
        self._goal = goal
        self._ctx = ctx
        self._plan = plan
        self._planner = planner or _default_planner
        self._replanner = replanner or _default_replanner
        self._runner = runner
        self._step_hook = step_hook
        self._budget = budget or Budget.from_settings()
        self._eligibility = eligibility
        self._store = store
        self._trace_writer = trace_writer
        self._plan_mode = bool(plan_mode)
        self._clarify_answer = clarify_answer
        self._repeat_threshold = repeat_threshold
        self._clock = clock
        self._use_loop = use_loop

    # -- helpers ------------------------------------------------------------ #
    def _store_or_default(self) -> Any:
        if self._store is not None:
            return self._store
        from forgeflow.hitl.pending import get_pending_store

        return get_pending_store()

    def _runner_or_default(self) -> StepRunner:
        if self._runner is not None:
            return self._runner
        return _default_step_runner(self._ctx, step_hook=self._step_hook)

    async def _write_trace(self, row: dict[str, Any]) -> None:
        writer = self._trace_writer or _default_trace_writer
        try:
            await writer(row)
        except Exception:  # noqa: BLE001 — a trace write must never break a run
            logger.warning("agent loop trace write skipped (best-effort)", exc_info=True)

    def _trace_row(
        self,
        plan: Plan,
        step: PlanStep,
        obs: Observation,
        iteration: int,
        replans: int,
    ) -> dict[str, Any]:
        started_at = datetime.now(timezone.utc).isoformat()
        step_id = step.step_id or f"{self._ctx.run_id}:0:{iteration}"
        invocation: dict[str, Any] = {
            "run_id": self._ctx.run_id,
            "step_id": step_id,
            "agent_id": None,
            "tool": step.tool or step.step_type,
            "arguments_hash": None,
            "tenant_id": self._ctx.tenant_id,
            "data_scope": None,
            "policy_decision": "not_evaluated",
            "approval_id": obs.pending_id,
            "status": obs.kind,
            "executed": obs.kind == OBS_OK,
            "invoked": obs.kind in (OBS_OK, OBS_TOOL_ERROR, OBS_VALIDATION_FAILED),
            "development_stub": False,
            "provider": "agent-loop",
            "summary": (obs.detail or "")[:200],
            "result_ref": None,
            "latency_ms": obs.latency_ms,
            "error": obs.detail if obs.kind != OBS_OK else None,
            "started_at": started_at,
            "attempt": 0,
            "payload": obs.raw if isinstance(obs.raw, dict) else None,
            "actor_user_id": self._ctx.user_id,
            "actor_role": self._ctx.role,
            # T28 additive linkage — rides the existing JSONB ``output`` column.
            "plan_id": plan.plan_id,
            "iteration": iteration,
            "replan": replans,
            "observation_kind": obs.kind,
            "step_type": step.step_type,
        }
        return {
            "invocation": invocation,
            "args": dict(step.args or {}),
            "run_id": self._ctx.run_id,
            "tenant_id": self._ctx.tenant_id,
            "step_id": step_id,
            "plan_id": plan.plan_id,
        }

    async def _clarify(self, step: PlanStep, plan: Plan) -> Observation:
        """Handle a ``clarify`` step — ask a human, reusing T21 (红线 21)."""
        if self._clarify_answer is not None:
            answer = self._clarify_answer(step, plan)
            if inspect.isawaitable(answer):
                answer = await answer
            if answer:
                return Observation(
                    kind=OBS_OK,
                    step_id=step.step_id,
                    detail=str(answer),
                    progress=True,
                )
        if not self._ctx.tenant_id:
            return Observation(
                kind=OBS_PERMISSION_DENIED,
                step_id=step.step_id,
                detail="tenant required to clarify (fail closed, 红线 5)",
            )
        store = self._store_or_default()
        pending_id = f"pa-{self._ctx.run_id}-clarify-{step.step_id or 'x'}"
        record = store.create(
            self._ctx.tenant_id,
            run_id=self._ctx.run_id,
            kind="clarify",
            payload={"question": step.purpose or "请澄清任务目标", "plan_id": plan.plan_id},
            pending_id=pending_id,
        )
        return Observation(
            kind=OBS_AWAITING_APPROVAL,
            step_id=step.step_id,
            detail="等待人工澄清（T21 pending_actions）",
            progress=False,
            pending_id=record.pending_id,
        )

    async def _ensure_plan_approval(self, plan: Plan) -> str | None:
        """Plan-mode gate: require a resolved T21 approval for a risky plan.

        Returns the pending id when the plan must still wait (pause), or ``None``
        when it is already approved (or no tenant — which fails closed as a
        pending that can never be resolved, i.e. it pauses).
        """
        if not self._ctx.tenant_id:
            return None  # no tenant ⇒ the risk gate below cannot be satisfied; the
            # caller's plan_mode decision is recorded as DENIED before this is used
        store = self._store_or_default()
        from forgeflow.hitl.pending import RESOLVED

        pending_id = f"pa-{self._ctx.run_id}-plan-{plan.plan_id}"
        existing = store.get(self._ctx.tenant_id, pending_id)
        if existing is not None:
            if existing.status == RESOLVED:
                return None
            return existing.pending_id
        record = store.create(
            self._ctx.tenant_id,
            run_id=self._ctx.run_id,
            kind="approve_diff",
            payload={"plan": plan.to_dict(), "reason": "高风险计划需人工批准后执行"},
            pending_id=pending_id,
        )
        return record.pending_id

    async def _act(self, step: PlanStep, plan: Plan) -> Observation:
        if step.step_type == STEP_CLARIFY:
            return await self._clarify(step, plan)
        runner = self._runner_or_default()
        state = LoopState(run_id=self._ctx.run_id, plan_id=plan.plan_id)
        result = await runner(step, state)
        return _coerce_observation(result, step)

    # -- run ---------------------------------------------------------------- #
    async def run(self) -> LoopResult:
        """Drive the loop to a terminal state (or an honest pause)."""
        if self._use_loop is None:
            self._use_loop = agent_loop_enabled()
        if not self._use_loop:
            # 零回归: flag off ⇒ honest no-op, nothing executed, nothing traced.
            return LoopResult(
                status=DISABLED,
                path=PATH_DISABLED,
                reason="agent.loop 未开启（默认关闭）——未执行，行为与今日一致",
            )

        tracker = BudgetTracker(self._budget, clock=self._clock)
        guard = LoopGuard(self._repeat_threshold)
        eligibility = (
            self._eligibility
            if self._eligibility is not None
            else eligible_tools(self._ctx.role)
        )

        plan = self._plan or await self._planner(self._goal)
        plan_ids: list[str] = [plan.plan_id]
        plans: list[dict[str, Any]] = [plan.to_dict()]
        completed: list[dict[str, Any]] = []
        trace: list[dict[str, Any]] = []
        iteration = 0
        replans = 0

        # Plan mode (optional): a risky plan is shown and approved via T21 first.
        if self._plan_mode and has_risky_steps(plan):
            if not self._ctx.tenant_id:
                return LoopResult(
                    status=DENIED,
                    plan_id=plan.plan_id,
                    plan_ids=plan_ids,
                    plans=plans,
                    reason="高风险计划需要人工批准，但缺少 tenant（fail-closed，红线 5/21）",
                )
            pending_id = await self._ensure_plan_approval(plan)
            if pending_id is not None:
                return LoopResult(
                    status=AWAITING_APPROVAL,
                    plan_id=plan.plan_id,
                    plan_ids=plan_ids,
                    plans=plans,
                    unfinished=[s.to_dict() for s in plan.steps],
                    pending_id=pending_id,
                    reason="高风险计划等待人工批准（T21）",
                )

        index = 0
        while index < len(plan.steps):
            # --- SINGLE budget enforcement point (红线 18) ------------------ #
            breach = tracker.check()
            if breach is not None:
                return LoopResult(
                    status=BUDGET_EXCEEDED,
                    plan_id=plan.plan_id,
                    plan_ids=plan_ids,
                    plans=plans,
                    completed=completed,
                    unfinished=[s.to_dict() for s in plan.steps[index:]],
                    replans=replans,
                    iterations=iteration,
                    trace=trace,
                    reason=breach.message,
                    budget=tracker.to_dict(),
                )

            step = plan.steps[index]

            # --- eligibility (fail-closed, 红线 5) -------------------------- #
            if _step_tool(step) and step.tool not in eligibility:
                obs = Observation(
                    kind=OBS_PERMISSION_DENIED,
                    step_id=step.step_id,
                    tool=step.tool,
                    detail=(
                        f"工具 '{step.tool}' 不在本角色资格集合内"
                        "（重规划不得扩大权限，fail-closed，红线 5）"
                    ),
                )
                iteration += 1
                row = self._trace_row(plan, step, obs, iteration, replans)
                await self._write_trace(row)
                trace.append(row)
                return LoopResult(
                    status=DENIED,
                    plan_id=plan.plan_id,
                    plan_ids=plan_ids,
                    plans=plans,
                    completed=completed,
                    unfinished=[s.to_dict() for s in plan.steps[index:]],
                    replans=replans,
                    iterations=iteration,
                    trace=trace,
                    reason=obs.detail,
                    budget=tracker.to_dict(),
                )

            # --- Act ------------------------------------------------------ #
            iteration += 1
            obs = await self._act(step, plan)
            tracker.note_step()
            tracker.note_tokens(obs.tokens)
            row = self._trace_row(plan, step, obs, iteration, replans)
            await self._write_trace(row)
            trace.append(row)

            # --- Observe: pause for a human ------------------------------- #
            if obs.kind == OBS_AWAITING_APPROVAL:
                return LoopResult(
                    status=AWAITING_APPROVAL,
                    plan_id=plan.plan_id,
                    plan_ids=plan_ids,
                    plans=plans,
                    completed=completed,
                    unfinished=[s.to_dict() for s in plan.steps[index:]],
                    replans=replans,
                    iterations=iteration,
                    trace=trace,
                    pending_id=obs.pending_id,
                    reason=obs.detail or "等待人工",
                    budget=tracker.to_dict(),
                )

            # --- Observe: matched the plan -------------------------------- #
            if obs.kind == OBS_OK:
                completed.append(step.to_dict())
                signal = guard.observe(_step_tool(step) or step.step_type, step.args, progress=obs.progress)
                if signal is not None:
                    return LoopResult(
                        status=LOOP_DETECTED,
                        plan_id=plan.plan_id,
                        plan_ids=plan_ids,
                        plans=plans,
                        completed=completed,
                        unfinished=[s.to_dict() for s in plan.steps[index + 1:]],
                        replans=replans,
                        iterations=iteration,
                        trace=trace,
                        reason=signal.message,
                        loop_signal=signal.to_dict(),
                        budget=tracker.to_dict(),
                    )
                index += 1
                continue

            # --- Observe: mismatch ⇒ Replan ------------------------------- #
            next_plan = await self._replanner(plan, obs)
            if next_plan is None:
                return LoopResult(
                    status=FAILED,
                    plan_id=plan.plan_id,
                    plan_ids=plan_ids,
                    plans=plans,
                    completed=completed,
                    unfinished=[s.to_dict() for s in plan.steps[index:]],
                    replans=replans,
                    iterations=iteration,
                    trace=trace,
                    reason=f"观察与预期不符（{_OBS_LABEL.get(obs.kind, obs.kind)}），且无法重规划（fail-closed）",
                    budget=tracker.to_dict(),
                )

            tracker.note_replan()
            replans += 1

            rejected = [s for s in next_plan.steps if _step_tool(s) and s.tool not in eligibility]
            if rejected:
                obs2 = Observation(
                    kind=OBS_PERMISSION_DENIED,
                    step_id=rejected[0].step_id,
                    tool=rejected[0].tool,
                    detail=(
                        f"重规划提出资格集合外工具 '{rejected[0].tool}'，已拒绝执行"
                        "（不得扩大权限，fail-closed，红线 5）"
                    ),
                )
                iteration += 1
                row = self._trace_row(next_plan, rejected[0], obs2, iteration, replans)
                await self._write_trace(row)
                trace.append(row)
                return LoopResult(
                    status=DENIED,
                    plan_id=next_plan.plan_id,
                    plan_ids=[*plan_ids, next_plan.plan_id],
                    plans=[*plans, next_plan.to_dict()],
                    completed=completed,
                    unfinished=[s.to_dict() for s in next_plan.steps],
                    replans=replans,
                    iterations=iteration,
                    trace=trace,
                    reason=obs2.detail,
                    budget=tracker.to_dict(),
                )

            if not next_plan.steps:
                return LoopResult(
                    status=FAILED,
                    plan_id=next_plan.plan_id,
                    plan_ids=[*plan_ids, next_plan.plan_id],
                    plans=[*plans, next_plan.to_dict()],
                    completed=completed,
                    replans=replans,
                    iterations=iteration,
                    trace=trace,
                    reason="重规划产出空计划，无以为继（fail-closed）",
                    budget=tracker.to_dict(),
                )

            plan = next_plan
            plan_ids.append(plan.plan_id)
            plans.append(plan.to_dict())
            index = 0

        return LoopResult(
            status=DONE,
            plan_id=plan.plan_id,
            plan_ids=plan_ids,
            plans=plans,
            completed=completed,
            replans=replans,
            iterations=iteration,
            trace=trace,
            reason="计划执行完毕",
            budget=tracker.to_dict(),
        )


# --------------------------------------------------------------------------- #
# Module helpers                                                               #
# --------------------------------------------------------------------------- #
def _step_tool(step: PlanStep) -> str:
    """The tool a step drives (``""`` for a step that drives no tool)."""
    return (step.tool or "").strip()


def _coerce_observation(result: Any, step: PlanStep) -> Observation:
    """Normalise whatever a runner returned into an :class:`Observation`.

    Accepts an :class:`Observation` (verbatim), a mapping (``kind`` + fields), or
    ``None``/``True``/``False`` (ok / ok / tool-error). Anything unexpected is an
    honest ``tool_error`` — never silently upgraded to success.
    """
    if isinstance(result, Observation):
        if not result.step_id:
            result.step_id = step.step_id
        if not result.tool:
            result.tool = step.tool
        return result
    if isinstance(result, dict):
        return Observation(
            kind=str(result.get("kind") or OBS_OK),
            step_id=str(result.get("step_id") or step.step_id),
            tool=str(result.get("tool") or step.tool),
            detail=str(result.get("detail") or ""),
            progress=bool(result.get("progress", True)),
            tokens=int(result.get("tokens") or 0),
            pending_id=result.get("pending_id"),
            raw=result.get("raw"),
        )
    if result is None or result is True:
        return Observation(kind=OBS_OK, step_id=step.step_id, tool=step.tool)
    if result is False:
        return Observation(
            kind=OBS_TOOL_ERROR,
            step_id=step.step_id,
            tool=step.tool,
            detail="runner reported failure",
            progress=False,
        )
    return Observation(
        kind=OBS_TOOL_ERROR,
        step_id=step.step_id,
        tool=step.tool,
        detail=f"runner returned an unexpected value: {type(result).__name__}",
        progress=False,
    )


async def run_agent_loop(
    goal: str,
    *,
    ctx: RunContext,
    plan: Plan | None = None,
    planner: Planner | None = None,
    replanner: Replanner | None = None,
    runner: StepRunner | None = None,
    step_hook: Any | None = None,
    budget: Budget | None = None,
    eligibility: frozenset[str] | None = None,
    store: Any | None = None,
    trace_writer: TraceWriter | None = None,
    plan_mode: bool = False,
    clarify_answer: Callable[[PlanStep, Plan], Any] | None = None,
    repeat_threshold: int = DEFAULT_REPEAT_THRESHOLD,
    clock: Any | None = None,
    use_loop: bool | None = None,
) -> LoopResult:
    """Convenience entry: build an :class:`AgentLoop` and run it once."""
    loop = AgentLoop(
        goal,
        ctx=ctx,
        plan=plan,
        planner=planner,
        replanner=replanner,
        runner=runner,
        step_hook=step_hook,
        budget=budget,
        eligibility=eligibility,
        store=store,
        trace_writer=trace_writer,
        plan_mode=plan_mode,
        clarify_answer=clarify_answer,
        repeat_threshold=repeat_threshold,
        clock=clock,
        use_loop=use_loop,
    )
    return await loop.run()
