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
.agent_runtime_mode``: ``auto`` | ``llm`` | ``deterministic``). ``llm`` runs
:func:`_llm_executor`, which asks the Supervisor model for the plan (one
batched call), gates and runs each step, then reflects on the result (one more
batched call) — see :mod:`forgeflow.runtime.llm_planner`. ``deterministic``
keeps :func:`_default_executor` exactly as it was, so the offline profile is
unchanged. Both paths apply the same RBAC + HITL gates and both report real
token usage through the ``TaskCreate.context["llm_usage"]`` contract.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from forgeflow.config import get_settings
from forgeflow.experience.extractor import ExperienceExtractor
from forgeflow.repositories import get_experience_repository, get_policy_repository
from forgeflow.repositories.base import new_id
from forgeflow.runtime.events import RunEventBus, get_event_bus
from forgeflow.validation.replan import decide_replan, record_replan_event
from forgeflow.validation.validator import validate

logger = logging.getLogger(__name__)

# Default multi-agent step plan (Supervisor → Research → Data → Code).
_DEFAULT_STEPS: list[dict[str, Any]] = [
    {"tool": "research.search", "step_type": "agent", "note": "研究助手检索资料"},
    {"tool": "data.query", "step_type": "agent", "note": "数据分析师查询数据集"},
    {"tool": "code.run", "step_type": "agent", "note": "代码开发师执行并验证"},
]


@dataclass
class RequestContext:
    """Identity + tenant scope for a run (docs §10.5)."""

    tenant_id: str | None = None
    team_id: str | None = None
    user_id: str = "anonymous"
    role: str = "viewer"
    available_skills: list[str] = field(default_factory=list)


@dataclass
class TaskCreate:
    """A user intent submitted to the platform."""

    intent: str = ""
    title: str = ""
    workflow_type: str = "generic"
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunHandle:
    """Returned synchronously when a run is accepted/finished."""

    run_id: str
    thread_id: str
    status: str
    detail: dict[str, Any] = field(default_factory=dict)

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


_RUN_STORE = MemoryRunStore()


def get_run_store() -> MemoryRunStore:
    return _RUN_STORE


def reset_run_store() -> None:
    global _RUN_STORE
    _RUN_STORE = MemoryRunStore()


# --------------------------------------------------------------------------- #
# Runtime mode — LLM vs. deterministic executor (INC4 §A)                      #
# --------------------------------------------------------------------------- #
#: Accepted ``Settings.agent_runtime_mode`` values. "auto" resolves from the
#: configured provider; the other two pin the path explicitly.
_AGENT_RUNTIME_MODES: tuple[str, ...] = ("auto", "llm", "deterministic")


def resolve_agent_runtime_mode() -> str:
    """The **effective** agent-runtime mode: ``"llm"`` or ``"deterministic"``.

    ``Settings.agent_runtime_mode`` is read defensively (``getattr``) so this
    keeps working on a ``Settings`` that predates the field. ``auto`` resolves
    from the *configured provider*:

      * ``auto`` + ``llm_provider != "mock"`` ⇒ ``llm`` — a real provider is
        configured, so the Agent must actually use it.
      * ``auto`` + ``llm_provider in ("", "mock")`` ⇒ ``deterministic`` — the
        offline profile keeps the pre-INC4 platform graph byte-for-byte, so the
        668-case offline suite is unaffected.

    ``llm`` / ``deterministic`` pin the path regardless of provider (which is
    what lets a unit test exercise the LLM path against ``MockChatModel``).
    """
    settings = get_settings()
    raw = getattr(settings, "agent_runtime_mode", "auto")
    requested = str(raw or "auto").strip().lower()
    if requested not in _AGENT_RUNTIME_MODES:
        logger.warning("unknown agent_runtime_mode=%r; treating as 'auto'", raw)
        requested = "auto"
    if requested == "auto":
        provider = str(getattr(settings, "llm_provider", "") or "").strip().lower()
        return "deterministic" if provider in ("", "mock") else "llm"
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
    """
    from forgeflow.models import get_model
    from forgeflow.runtime.llm_planner import describe_model

    built: dict[str, Any] = {}
    for strong, label in ((True, "strong"), (False, "worker")):
        try:
            built[label] = get_model(strong=strong)
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
    from forgeflow.runtime.gate import check_tool_permission, describe_denial
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
        return await _default_executor(task, ctx, bus, run_id, policy_engine=policy_engine)

    planner = LLMPlanner(primary, alt_model=alt, allowed_tools=tool_allowlist)
    engine = policy_engine or PolicyEngine()
    simulate_failure = bool(task.context.get("simulate_failure")) or "失败" in task.intent
    steps: list[dict[str, Any]] = []
    errors: list[str] = []

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
        )
        runtime_meta["plan"] = plan.to_dict()
        await bus.emit(run_id, "run.plan", plan.to_dict())

        for index, plan_step in enumerate(plan.steps):
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
            steps.append(plan_step.to_payload(index, status="ok"))
            await bus.emit(
                run_id, "run.step.done", {"index": index, "tool": tool, "status": "ok"}
            )

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
        logger.warning("LLM runtime failed (%s); degrading to the deterministic executor", exc)
        runtime_meta["degraded"] = f"exception: {exc}"
        _stash_usage()
        return await _default_executor(task, ctx, bus, run_id, policy_engine=policy_engine)


async def _default_executor(
    task: TaskCreate,
    ctx: RequestContext,
    bus: RunEventBus,
    run_id: str,
    *,
    policy_engine: Any | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Deterministic multi-agent execution used when no graph is injected.

    Every step is risk-gated by the ``PolicyEngine`` (docs §6.3, QA V6): a
    high-risk tool call raises a HITL approval and **halts the run before the
    tool executes**. The route-level gate only sees ``workflows:execute``, so
    without this in-run check a dangerous tool would run unchecked.
    """
    from forgeflow.governance.policy_engine import PolicyEngine

    engine = policy_engine or PolicyEngine()
    simulate_failure = bool(task.context.get("simulate_failure")) or "失败" in task.intent
    steps: list[dict[str, Any]] = []
    errors: list[str] = []
    await bus.emit(run_id, "run.started", {"intent": task.intent, "workflow_type": task.workflow_type})
    for index, step in enumerate(_DEFAULT_STEPS):
        tool = str(step["tool"])

        # B5 (INC2-21): RBAC re-check FIRST. The route gate only ever saw
        # "workflows:execute"; a role that may start a run can still be barred
        # from a specific tool. Denied ⇒ run.error and return *before* the tool
        # (and before the risk gate) is reached.
        from forgeflow.runtime.gate import check_tool_permission, describe_denial

        if not check_tool_permission(ctx.role, tool):
            errors.append(describe_denial(ctx.role, tool))
            await bus.emit(
                run_id,
                "run.error",
                {"message": errors[-1], "tool": tool, "reason": "rbac_denied"},
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
            {"index": index, "tool": tool, "note": step["note"], "status": "running"},
        )
        await asyncio.sleep(0)  # yield to the event loop so SSE can flush
        steps.append({**step, "index": index, "status": "ok"})
        await bus.emit(
            run_id,
            "run.step.done",
            {"index": index, "tool": tool, "status": "ok"},
        )
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


def _default_usage_model() -> str:
    """Model name to assume for a usage record that does not name one.

    Chosen from the configured provider so a mock/self-hosted deployment is
    never accidentally billed at a cloud rate (``mock``/``ollama*`` → ``0.0``).
    """
    settings = get_settings()
    provider = (settings.llm_provider or "").strip().lower()
    if provider == "ollama":
        return settings.ollama_model
    if provider == "anthropic":
        return settings.anthropic_model
    if provider == "mock":
        return "mock"
    return settings.openai_model


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


async def run_task(
    task: TaskCreate,
    ctx: RequestContext,
    *,
    graph: Any | None = None,
    bus: RunEventBus | None = None,
    experience_repo: Any | None = None,
    policy_repo: Any | None = None,
    policy_engine: Any | None = None,
) -> RunHandle:
    """Execute a task to a terminal state and extract its experience.

    Returns a ``RunHandle`` whose ``detail`` carries outcome/steps/experience_id.
    """
    bus = bus or get_event_bus()
    exp_repo = experience_repo or get_experience_repository()
    pol_repo = policy_repo or get_policy_repository()

    run_id = new_id()
    thread_id = new_id()

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
    except Exception as exc:  # noqa: BLE001 — context is an enhancement, not a gate
        logger.warning("context build failed, continuing without it: %s", exc)

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
        executor = _llm_executor if runtime_mode == "llm" else _default_executor
        steps, errors = await executor(task, ctx, bus, run_id, policy_engine=policy_engine)

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
    attempt = 0
    while not verdict.success:
        decision = decide_replan(verdict, attempt)
        await bus.emit(run_id, "replan", record_replan_event(decision))
        if decision.should_replan:
            attempt += 1
            # Re-run with the *same* executor that produced the first attempt
            # (the LLM path replans through the LLM too; the graph path keeps
            # its historical deterministic replan). The plan/reflection caches
            # make a replan of an identical plan free of extra LLM calls.
            retry_executor = executor or _default_executor
            steps_retry, errors = await retry_executor(
                task, ctx, bus, run_id, policy_engine=policy_engine
            )
            run_state["steps"] = steps_retry
            run_state["errors"] = errors
            run_state["status"] = "failed" if errors else "completed"
            verdict = validate(run_state)
            continue
        if decision.escalate_hitl:
            await _escalate_hitl(pol_repo, ctx, run_id, task, verdict)
        break

    status = "completed" if verdict.success else "failed"

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
        created_at=datetime.now(timezone.utc).isoformat(),
        completed_at=datetime.now(timezone.utc).isoformat(),
        experience_id=experience.id,
        total_tokens=int(cost_summary["total_tokens"]),
        total_cost_usd=float(cost_summary["total_cost_usd"]),
        cost_by_agent=dict(cost_summary["by_agent"]),
        runtime_mode=runtime_mode,
    )
    get_run_store().save(record)

    llm_meta = task.context.get("llm_runtime")

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
        },
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
