"""LLM-driven Agent execution: **Planning** + **Reflection** (INC4 §A).

Why this module exists
----------------------
``orchestrator._default_executor`` is the default path behind ``POST /tasks``.
It walks a **hard-coded** ``_DEFAULT_STEPS`` constant, performs no LLM call, no
planning and no reflection, and marks every step ``ok`` unconditionally. The
configured provider (Ollama) was therefore *never* used by the agent's main
execution path — the platform had an LLM wired up but the agent did not use it.

This module supplies the two LLM steps that make the runtime actually use the
provider, while keeping every cost/rate guard-rail the architecture mandates:

  * **Planning** — the Supervisor turns the intent into an execution plan. The
    plan's steps come from the *model's* structured JSON, not from a constant.
  * **Reflection** — the Supervisor judges the executed plan and its result.

Hard constraints honoured here (docs §K5 / goal.md)
--------------------------------------------------
1. **One batched call per phase.** A real Ollama call costs ~19 s on the target
   box (``qwen2.5vl:3b``). Planning = exactly one call that yields the *whole*
   plan; reflection = exactly one call. There is **no** per-step LLM call.
2. **Caching.** Identical (workflow_type, intent, skills) planning requests and
   identical (intent, plan, errors) reflection requests are served from a small
   in-process TTL cache, so a replan loop never re-pays for the same call.
3. **Rule fallback.** Any failure — provider down, unknown model, unparseable
   reply — degrades to the deterministic plan supplied by the caller
   (``fallback_steps``) instead of raising. The offline profile therefore keeps
   working exactly as before.
4. **Untrusted plan output.** Model-named tools are filtered against the set of
   tools the calling role may actually execute (``allowed_tools``) and against
   the platform's known tool catalogue. A hallucinated tool name is *dropped*
   and reported, never executed. The RBAC + HITL gates still run on every
   surviving step (see ``orchestrator._llm_executor``) — this is defence in
   depth, not a replacement.

The module is import-safe offline: the only third-party import at module scope
is ``langchain_core.messages`` (already a hard dependency of
``forgeflow.models.provider``); no provider SDK is imported here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

logger = logging.getLogger(__name__)

__all__ = [
    "ExecutionPlan",
    "LLMPlanner",
    "PlanStep",
    "Reflection",
    "Usage",
    "clear_caches",
    "clear_plan_cache",
    "describe_model",
    "known_plan_tools",
]

# --------------------------------------------------------------------------- #
# Plan cache                                                                   #
# --------------------------------------------------------------------------- #
#: TTL for a cached plan/reflection (seconds). Long enough to absorb a replan
#: loop and repeated identical tasks, short enough to pick up a changed prompt.
_CACHE_TTL_SECONDS: float = 600.0
#: Hard cap on cache entries (small — this is a latency guard, not a store).
_CACHE_MAX_ENTRIES: int = 256
_PLAN_CACHE: dict[str, tuple[float, ExecutionPlan]] = {}
_REFLECT_CACHE: dict[str, tuple[float, Reflection]] = {}


def _cache_key(*parts: Any) -> str:
    """Stable short key for a cache lookup."""
    joined = "\x1f".join(str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


def _cache_get(store: dict[str, tuple[float, Any]], key: str) -> Any | None:
    hit = store.get(key)
    if hit is None:
        return None
    at, value = hit
    if time.monotonic() - at > _CACHE_TTL_SECONDS:
        store.pop(key, None)
        return None
    return value


def _cache_put(store: dict[str, tuple[float, Any]], key: str, value: Any) -> None:
    if len(store) >= _CACHE_MAX_ENTRIES:
        # Drop the oldest entry (FIFO is fine for a latency cache).
        oldest = min(store, key=lambda k: store[k][0])
        store.pop(oldest, None)
    store[key] = (time.monotonic(), value)


def clear_plan_cache() -> None:
    """Drop every cached plan (tests + a manual "force a fresh plan" hook)."""
    _PLAN_CACHE.clear()


def clear_caches() -> None:
    """Drop every cached plan *and* reflection."""
    _PLAN_CACHE.clear()
    _REFLECT_CACHE.clear()


# --------------------------------------------------------------------------- #
# Tool allow-list                                                              #
# --------------------------------------------------------------------------- #
def known_plan_tools() -> frozenset[str]:
    """Every tool the platform itself ships and may name in a plan.

    Union of the runtime's platform steps, the skill catalogue and the tools
    that require an explicit grant (``payment.transfer`` …). A model-named tool
    outside this set is a hallucination and is dropped before execution.
    """
    from forgeflow.runtime.gate import (
        PLATFORM_TOOL_CATALOGUE,
        PLATFORM_TOOLS,
        TOOL_PERMISSION_MAP,
    )

    return frozenset(PLATFORM_TOOL_CATALOGUE | PLATFORM_TOOLS | set(TOOL_PERMISSION_MAP))


# --------------------------------------------------------------------------- #
# Result types                                                                 #
# --------------------------------------------------------------------------- #
@dataclass
class Usage:
    """Token usage of a single LLM call (real numbers, or ``0`` when unknown)."""

    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
        )


@dataclass
class PlanStep:
    """One step of an execution plan, as produced by the Supervisor model."""

    tool: str
    note: str = ""
    step_type: str = "agent"
    rationale: str = ""

    def to_payload(self, index: int, *, status: str = "ok") -> dict[str, Any]:
        return {
            "tool": self.tool,
            "step_type": self.step_type,
            "note": self.note,
            "index": index,
            "status": status,
        }


@dataclass
class ExecutionPlan:
    """The Supervisor's plan for one run."""

    steps: list[PlanStep]
    reasoning: str = ""
    #: ``"llm"`` when the steps came from the model, ``"fallback"`` when the
    #: deterministic plan was used (offline / parse failure / empty model plan).
    source: str = "fallback"
    #: Tool names the model named that were not permitted — dropped, never run.
    dropped: list[str] = field(default_factory=list)
    cached: bool = False
    usage: Usage = field(default_factory=Usage)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "reasoning": self.reasoning,
            "cached": self.cached,
            "dropped_tools": list(self.dropped),
            "steps": [s.tool for s in self.steps],
            "usage": {"input_tokens": self.usage.input_tokens,
                      "output_tokens": self.usage.output_tokens},
        }


@dataclass
class Reflection:
    """The Supervisor's judgement of an executed plan."""

    #: ``None`` ⇒ the model gave no usable verdict; the deterministic
    #: ``validation.validator.validate`` stays authoritative.
    success: bool | None = None
    score: float | None = None
    summary: str = ""
    reasons: list[str] = field(default_factory=list)
    source: str = "fallback"
    cached: bool = False
    usage: Usage = field(default_factory=Usage)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "score": self.score,
            "summary": self.summary,
            "reasons": list(self.reasons),
            "source": self.source,
            "cached": self.cached,
            "usage": {"input_tokens": self.usage.input_tokens,
                      "output_tokens": self.usage.output_tokens},
        }


# --------------------------------------------------------------------------- #
# Structured-output schemas (JSON-schema dicts → Ollama's native `format`)      #
# --------------------------------------------------------------------------- #
_PLAN_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tool": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["tool"],
            },
        },
    },
    "required": ["steps"],
}

_REFLECT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "success": {"type": "boolean"},
        "score": {"type": "number"},
        "summary": {"type": "string"},
        "reasons": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["success"],
}


def describe_model(model: Any) -> dict[str, Any]:
    """Best-effort identity of a chat model — used as *evidence* that the
    runtime really built the configured provider and did not silently degrade.
    """
    if model is None:
        return {"class": None, "llm_type": None, "model": None}
    base_url = getattr(model, "base_url", None)
    return {
        "class": type(model).__name__,
        "llm_type": getattr(model, "_llm_type", None),
        "model": getattr(model, "model", None),
        "base_url": str(base_url) if base_url else None,
    }


# --------------------------------------------------------------------------- #
# Lenient JSON extraction / usage extraction                                   #
# --------------------------------------------------------------------------- #
def _extract_json(text: str | None) -> dict[str, Any] | None:
    """Pull the first JSON object out of a model reply (tolerates ``` fences).

    A local copy of the same lenient parser used by
    ``skills/candidate_compiler`` — kept here so the runtime does not depend on
    the skills layer, and so a qwen ``thinking`` trace that wraps the JSON in
    prose still parses.
    """
    if not text:
        return None
    body = str(text).strip()
    if body.startswith("```"):
        body = body.strip("`")
        if body[:4].lower() == "json":
            body = body[4:]
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(body[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _extract_usage(message: Any) -> Usage:
    """Real token usage from an ``AIMessage`` (LangChain standard + Ollama)."""
    usage = getattr(message, "usage_metadata", None)
    if isinstance(usage, dict) and (
        usage.get("input_tokens") is not None or usage.get("output_tokens") is not None
    ):
        return Usage(
            int(usage.get("input_tokens") or 0),
            int(usage.get("output_tokens") or 0),
        )
    meta = getattr(message, "response_metadata", None)
    if isinstance(meta, dict):
        return Usage(
            int(meta.get("prompt_eval_count") or 0),
            int(meta.get("eval_count") or 0),
        )
    return Usage()


# --------------------------------------------------------------------------- #
# Prompt builders                                                              #
# --------------------------------------------------------------------------- #
_PLAN_SYSTEM = (
    "你是企业多智能体平台（AgentFlow）的 Supervisor。你的职责是把用户意图拆解为"
    "一份可执行的多智能体计划。"
)

_PLAN_INSTRUCTION = (
    "请为下面的任务制定执行计划。\n"
    "硬性规则：\n"
    "1. 只能使用下列允许的工具：{tools}。不得发明工具名。\n"
    "2. 计划 2-5 步，按依赖顺序排列；若任务涉及数据检索、分析或产物生成，"
    "请把对应工具一并纳入计划（如 research.search / data.query / analysis.score / "
    "report.render / code.run），不要只给一步。\n"
    "3. 只输出一个 JSON 对象，不要任何解释、不要 markdown 代码块：\n"
    '   {{"reasoning": "一句话说明拆解依据", '
    '"steps": [{{"tool": "工具名", "note": "该步要做什么"}}]}}\n'
    "任务意图：{intent}\n"
    "工作流类型：{workflow_type}\n"
    "可用技能：{skills}"
)

_REFLECT_SYSTEM = (
    "你是企业多智能体平台（AgentFlow）的 Supervisor，负责对执行结果做复盘判定。"
)

_REFLECT_INSTRUCTION = (
    "请判定下面这次执行是否完成了用户意图。\n"
    "只输出一个 JSON 对象，不要任何解释、不要 markdown 代码块：\n"
    '{{"success": true|false, "score": 0-1 的小数, "summary": "一句话结论", '
    '"reasons": ["判定依据"]}}\n'
    "任务意图：{intent}\n"
    "执行计划：{plan}\n"
    "已执行步骤：{steps}\n"
    "运行时错误：{errors}"
)


def _compact(text: Any, limit: int = 400) -> str:
    return str(text or "").strip()[:limit]


# --------------------------------------------------------------------------- #
# The planner                                                                  #
# --------------------------------------------------------------------------- #
class LLMPlanner:
    """Planning + reflection over a LangChain chat model.

    Args:
        model: the primary chat model (built via ``forgeflow.models.get_model``).
        alt_model: an optional secondary model tried when the primary call
            *raises* (e.g. the configured strong model is not pulled locally).
            Passing the worker model here is what makes a mis-pulled strong
            model degrade to a real answer instead of silently skipping the LLM.
        allowed_tools: tools the calling role may execute. Model-named tools
            outside this set are dropped. Defaults to every known plan tool.
        use_cache: honour the in-process plan/reflection cache.
    """

    def __init__(
        self,
        model: Any,
        *,
        alt_model: Any | None = None,
        allowed_tools: list[str] | tuple[str, ...] | None = None,
        use_cache: bool = True,
    ) -> None:
        self._models: list[Any] = []
        seen: set[int] = set()
        for candidate in (model, alt_model):
            if candidate is not None and id(candidate) not in seen:
                self._models.append(candidate)
                seen.add(id(candidate))
        if not self._models:
            raise ValueError("LLMPlanner requires at least one model")
        self._allowed: frozenset[str] = frozenset(allowed_tools) if allowed_tools else known_plan_tools()
        self._use_cache = use_cache
        self.usage_log: list[dict[str, Any]] = []

    # -- public API ---------------------------------------------------------- #
    @property
    def models(self) -> list[Any]:
        return list(self._models)

    async def plan(
        self,
        *,
        intent: str,
        workflow_type: str,
        fallback_steps: list[dict[str, Any]],
        available_skills: list[str] | None = None,
    ) -> ExecutionPlan:
        """Produce the run's execution plan (one batched LLM call)."""
        skills = list(available_skills or [])
        cache_key = _cache_key("plan", workflow_type, intent, tuple(sorted(skills)))
        if self._use_cache:
            cached = _cache_get(_PLAN_CACHE, cache_key)
            if isinstance(cached, ExecutionPlan):
                logger.info("LLMPlanner: plan cache HIT (%d steps)", len(cached.steps))
                return ExecutionPlan(
                    steps=list(cached.steps),
                    reasoning=cached.reasoning,
                    source=cached.source,
                    dropped=list(cached.dropped),
                    cached=True,
                    usage=Usage(),
                )

        prompt = _PLAN_INSTRUCTION.format(
            tools=", ".join(sorted(self._allowed)),
            intent=_compact(intent, 600),
            workflow_type=workflow_type or "generic",
            skills=", ".join(skills) or "无",
        )
        data, usage = await self._call_json(
            system=_PLAN_SYSTEM,
            prompt=prompt,
            schema=_PLAN_JSON_SCHEMA,
            agent="supervisor",
            phase="plan",
        )

        plan = self._build_plan(data, fallback_steps, usage)
        if self._use_cache and data is not None:
            _cache_put(_PLAN_CACHE, cache_key, plan)
        return plan

    async def reflect(
        self,
        *,
        intent: str,
        plan_signature: str,
        steps: list[dict[str, Any]],
        errors: list[str],
    ) -> Reflection:
        """Judge the executed plan (one batched LLM call)."""
        step_sig = "+".join(str(s.get("tool", "")) for s in steps)
        err_sig = "|".join(str(e) for e in errors)
        cache_key = _cache_key("reflect", intent, plan_signature, step_sig, err_sig)
        if self._use_cache:
            cached = _cache_get(_REFLECT_CACHE, cache_key)
            if isinstance(cached, Reflection):
                logger.info("LLMPlanner: reflection cache HIT")
                return Reflection(
                    success=cached.success,
                    score=cached.score,
                    summary=cached.summary,
                    reasons=list(cached.reasons),
                    source=cached.source,
                    cached=True,
                    usage=Usage(),
                )

        step_desc = "; ".join(
            f"{s.get('index', i)}.{s.get('tool', '?')}[{s.get('status', '?')}]"
            for i, s in enumerate(steps)
        )
        prompt = _REFLECT_INSTRUCTION.format(
            intent=_compact(intent, 400),
            plan=_compact(plan_signature, 300),
            steps=_compact(step_desc, 600) or "（无步骤）",
            errors=_compact("; ".join(errors), 400) or "无",
        )
        data, usage = await self._call_json(
            system=_REFLECT_SYSTEM,
            prompt=prompt,
            schema=_REFLECT_JSON_SCHEMA,
            agent="reviewer",
            phase="reflect",
        )
        reflection = self._build_reflection(data, usage)
        if self._use_cache and data is not None:
            _cache_put(_REFLECT_CACHE, cache_key, reflection)
        return reflection

    # -- internals ----------------------------------------------------------- #
    async def _call_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        agent: str,
        phase: str,
    ) -> tuple[dict[str, Any] | None, Usage]:
        """Ask a model for a JSON object, bounded to ≤3 attempts.

        Strategy (the *only* reliable local path first):

          1. ``model.bind(format=<json-schema>)`` — Ollama's native constrained
             decoding. One call on the primary, one on the alt model if the
             primary raised.
          2. A plain prompt asking for JSON, parsed leniently.

        Returns ``(parsed_dict_or_None, usage)``; the accumulated usage of every
        attempt is returned so a failed/retried call is still billed honestly.
        """
        messages = [SystemMessage(content=system), HumanMessage(content=prompt)]
        total = Usage()

        attempts: list[tuple[Any, str]] = []
        for model in self._models:
            attempts.append((model, "format"))
        if self._models:
            attempts.append((self._models[0], "plain"))

        for model, mode in attempts:
            try:
                if mode == "format":
                    bound: Any = model.bind(format=schema)
                else:
                    bound = model
                message = await bound.ainvoke(messages)
            except Exception as exc:  # noqa: BLE001 — any provider hiccup degrades
                logger.warning(
                    "LLMPlanner[%s]: model %s (%s) failed: %s",
                    phase,
                    describe_model(model).get("model") or type(model).__name__,
                    mode,
                    exc,
                )
                continue

            usage = _extract_usage(message)
            total = total.add(usage)
            self._record(agent, model, usage)
            data = _extract_json(getattr(message, "content", ""))
            if data is None:
                logger.warning(
                    "LLMPlanner[%s]: unparseable reply from %s (%s); trying next path",
                    phase,
                    describe_model(model).get("model") or type(model).__name__,
                    mode,
                )
                continue
            model_id = describe_model(model)
            logger.info(
                "LLMPlanner[%s]: OK via %s mode=%s in/out=%d/%d",
                phase,
                model_id.get("model") or model_id.get("class"),
                mode,
                usage.input_tokens,
                usage.output_tokens,
            )
            return data, total

        logger.warning("LLMPlanner[%s]: every model/path failed — using rule fallback", phase)
        return None, total

    def _record(self, agent: str, model: Any, usage: Usage) -> None:
        """Append one real usage entry (the shape ``_record_usage`` consumes)."""
        info = describe_model(model)
        self.usage_log.append(
            {
                "agent": agent,
                "model": info.get("model") or info.get("class") or "unknown",
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
            }
        )

    def _build_plan(
        self,
        data: dict[str, Any] | None,
        fallback_steps: list[dict[str, Any]],
        usage: Usage,
    ) -> ExecutionPlan:
        """Coerce a model reply into an ``ExecutionPlan`` (or the fallback)."""
        if data is not None:
            raw_steps = data.get("steps")
            if isinstance(raw_steps, list):
                steps: list[PlanStep] = []
                dropped: list[str] = []
                for item in raw_steps:
                    tool, note = self._parse_step(item)
                    if not tool:
                        continue
                    if tool not in self._allowed:
                        dropped.append(tool)
                        logger.warning(
                            "LLMPlanner: dropping non-permitted tool %r from plan", tool
                        )
                        continue
                    steps.append(PlanStep(tool=tool, note=note))
                if steps:
                    return ExecutionPlan(
                        steps=steps,
                        reasoning=_compact(data.get("reasoning"), 600),
                        source="llm",
                        dropped=dropped,
                        usage=usage,
                    )
                logger.warning(
                    "LLMPlanner: model plan had no usable steps (dropped=%s) — "
                    "falling back to the deterministic plan",
                    dropped,
                )
        return self._fallback_plan(fallback_steps, usage)

    @staticmethod
    def _parse_step(item: Any) -> tuple[str, str]:
        """Accept ``{"tool","note"}`` or a bare ``"tool"`` string."""
        if isinstance(item, dict):
            tool = str(item.get("tool") or item.get("name") or "").strip()
            note = str(item.get("note") or item.get("description") or "").strip()
            return tool, note
        if isinstance(item, str):
            return item.strip(), ""
        return "", ""

    @staticmethod
    def _fallback_plan(
        fallback_steps: list[dict[str, Any]], usage: Usage
    ) -> ExecutionPlan:
        steps = [
            PlanStep(
                tool=str(s.get("tool") or ""),
                note=str(s.get("note") or ""),
                step_type=str(s.get("step_type") or "agent"),
            )
            for s in fallback_steps
            if str(s.get("tool") or "").strip()
        ]
        return ExecutionPlan(
            steps=steps,
            reasoning="deterministic fallback plan (LLM unavailable or unparseable)",
            source="fallback",
            usage=usage,
        )

    @staticmethod
    def _build_reflection(data: dict[str, Any] | None, usage: Usage) -> Reflection:
        """Coerce a model reply into a ``Reflection`` (``None`` verdict ⇒ rule)."""
        if data is None:
            return Reflection(source="fallback", usage=usage)
        raw_success = data.get("success")
        success: bool | None
        if isinstance(raw_success, bool):
            success = raw_success
        elif isinstance(raw_success, str) and raw_success.strip().lower() in ("true", "false"):
            success = raw_success.strip().lower() == "true"
        else:
            success = None

        score: float | None = None
        raw_score = data.get("score")
        try:
            if raw_score is not None:
                score = max(0.0, min(1.0, float(raw_score)))
        except (TypeError, ValueError):
            score = None

        reasons_raw = data.get("reasons")
        reasons = [str(r) for r in reasons_raw] if isinstance(reasons_raw, list) else []
        if success is None and score is None:
            return Reflection(source="fallback", usage=usage, summary=_compact(data.get("summary")))
        return Reflection(
            success=success,
            score=score,
            summary=_compact(data.get("summary"), 400),
            reasons=reasons,
            source="llm",
            usage=usage,
        )
