"""SupervisorAgent — routes workflow to the correct worker using structured LLM output.

The supervisor never calls tools directly. It reads the current stage + message
history and emits a RoutingDecision that the conditional edge uses to pick the
next node. This keeps routing logic deterministic and auditable.
"""

from __future__ import annotations

import logging
from typing import Any, Literal, cast

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, field_validator

from forgeflow.agents.base import BaseAgent
from forgeflow.state.workflow_state import WorkflowState

logger = logging.getLogger(__name__)

SUPERVISOR_SYSTEM = """You are the Supervisor Agent for ForgeFlow, a sales operations AI system.

Your job is to coordinate a team of specialist agents to process sales leads:
- researcher: Gathers web data about a company (funding, size, market)
- analyzer: Scores the lead (0-10), flags risks, determines ICP fit
- executor: Drafts proposals, writes to CRM, sends emails
- human_approval: Pauses for a human manager to review the proposal

Workflow stages:
1. qualify   → route to researcher (gather company intelligence)
2. research  → route to analyzer (score the lead)
3. analyze   → if score >= 4.0: route to executor (draft proposal); else FINISH (disqualified)
4. propose   → route to human_approval (await manager review)
5. approve   → route to executor (send proposal, update CRM)
6. done      → FINISH

Route to FINISH only when the workflow is complete or the lead is disqualified.
Valid next values are exactly: researcher, analyzer, executor, human_approval, FINISH — never a stage name like research or analyze.
Stage mapping is determinative: the current stage decides the route, and the task intent does NOT override it. A task that sounds like "gathering information" at stage research still routes to analyzer, because research is already done and scoring is next; a task that sounds like "scoring" at stage qualify still routes to researcher, because gathering comes first. Use intent only to understand what the task is about, never to pick a different stage's worker.
Always include a brief reasoning for your routing decision."""

WORKERS = Literal["researcher", "analyzer", "executor", "human_approval", "FINISH"]

# Stage-name aliases the model actually emits (2026-10-30 routing run
# b81d9c4: the model returned "research" at sup-r-02 and the whole
# evaluation aborted, and in production a structured-output Literal
# rejection would crash the graph). The supervisor's own system prompt
# names both stages (research/analyze/...) and workers
# (researcher/analyzer/...), so a stage name is a predictable model
# slip, not a new route — normalize it, never crash on it. "approve"
# is deliberately NOT mapped: the approve stage routes to executor,
# while human_approval is a different worker, so an ambiguous "approve"
# must fail validation rather than guess.
_ROUTE_ALIASES = {
    "research": "researcher",
    "analyze": "analyzer",
    "analysis": "analyzer",
    "execute": "executor",
    "execution": "executor",
    "propose": "human_approval",
    "human": "human_approval",
    "review": "human_approval",
    "finish": "FINISH",
    "done": "FINISH",
    "complete": "FINISH",
}


def normalize_route(value: str) -> str:
    """Map a stage-name / case slip to a valid worker route, else the value itself."""
    text = str(value or "").strip()
    if text in WORKERS.__args__:  # type: ignore[attr-defined]
        return text
    lowered = text.lower()
    # Case slip: "Researcher" / "FINISH" in any case is the valid route.
    for valid in WORKERS.__args__:  # type: ignore[attr-defined]
        if lowered == str(valid).lower():
            return str(valid)
    if lowered in _ROUTE_ALIASES:
        return _ROUTE_ALIASES[lowered]
    return text


class RoutingDecision(BaseModel):
    next: WORKERS
    reasoning: str

    @field_validator("next", mode="before")
    @classmethod
    def _normalize_next(cls, value: Any) -> Any:
        return normalize_route(value) if isinstance(value, str) else value


def deterministic_route_for_stage(stage: str, score: float | None = None) -> str | None:
    """Stage mapping as a rule, not a model judgement.

    Three consecutive LLM routing rounds (035fb2f 0/15, 3eff1b1 1/15,
    0ab7680 1/15 on Analyzer) show prompt wording does not fix a
    small local model that reads stage \"research\" as \"do research\".
    The documented mapping is already determinative — qualify →
    researcher, research → analyzer, analyze → executor/FINISH by
    score, propose → human_approval, approve → executor, done →
    FINISH — so enforce it in code and keep the LLM only for stages
    outside this map. LangGraph conditional edges are routinely
    rule functions; a stage map is not a model capability, and
    scoring it as one misstates what production routing is.
    Returns None for an unknown stage (LLM fallback).
    """
    mapping = {
        "qualify": "researcher",
        "research": "analyzer",
        "propose": "human_approval",
        "approve": "executor",
        "done": "FINISH",
    }
    if stage in mapping:
        return mapping[stage]
    if stage == "analyze":
        return "executor" if (score is not None and score >= 4.0) else "FINISH"
    return None


class SupervisorAgent(BaseAgent):
    def __init__(self, model: BaseChatModel, system_prompt: str | None = None) -> None:
        super().__init__(
            name="supervisor",
            model=model,
            tools=[],
            system_prompt=system_prompt or SUPERVISOR_SYSTEM,
        )
        # Supervisor uses structured output — no tool binding needed
        self._structured_model = model.with_structured_output(RoutingDecision)

    async def run(self, state: WorkflowState) -> dict:
        self._log_start(state)

        stage = state.get("current_stage", "qualify")
        errors = state.get("errors", [])
        analysis = state.get("analysis_scores", [])

        # Deterministic stage mapping first (see
        # deterministic_route_for_stage): for every documented stage
        # the route is already decided, so do not spend a model call
        # asking a small model to re-derive it — and do not let a
        # misread stage name override the workflow. The LLM path
        # below is only for stages outside the map.
        _score = None
        if analysis:
            try:
                _score = float(analysis[-1].get("score", 0))
            except Exception:  # noqa: BLE001 — non-numeric score ⇒ no threshold
                _score = None
        _deterministic = deterministic_route_for_stage(stage, _score)
        if _deterministic is not None:
            logger.info(
                "Supervisor routing (deterministic): %s → %s",
                stage,
                _deterministic,
            )
            stage_map = {
                "researcher": "research",
                "analyzer": "analyze",
                "executor": "propose" if stage in ("analyze", "research") else "execute",
                "human_approval": "approve",
                "FINISH": "done",
            }
            return {
                "next_agent": _deterministic,
                "current_stage": stage_map.get(_deterministic, stage),
                "messages": [
                    AIMessage(
                        content=f"[Supervisor] Routing to {_deterministic}: stage mapping is determinative for stage {stage}",
                        name="supervisor",
                    )
                ],
            }

        # Build context summary for routing. Intent-aware routing:
        # previously only stage / score / approval reached the prompt,
        # so the model could not distinguish two tasks at the same
        # stage (e.g. "gather company intelligence" vs "score this
        # lead") — the routing dataset therefore measured stage
        # mapping, not intent. Now a bounded task-intent summary
        # (last human message, max 500 chars, plus lead identity if
        # present) travels with the stage. Stage mapping itself is
        # unchanged; intent is additional evidence, never a new route.
        context_parts = [f"Current stage: {stage}"]
        lead_data = state.get("lead_data") or {}
        if isinstance(lead_data, dict):
            lead_name = str(lead_data.get("company") or lead_data.get("name") or "").strip()
            if lead_name:
                context_parts.append(f"Lead: {lead_name[:200]}")
        messages = state.get("messages") or []
        # Prefer the last human message (the task itself); a previous
        # supervisor AIMessage ("Routing to ...") is not task intent
        # and must never be mistaken for it.
        human_messages = [
            m
            for m in messages
            if getattr(m, "type", "") == "human" or type(m).__name__ == "HumanMessage"
        ]
        intent_source = human_messages[-1] if human_messages else None
        if intent_source is not None:
            content = getattr(intent_source, "content", "") or ""
            if isinstance(content, str) and content.strip():
                context_parts.append(f"Task intent: {content.strip()[:500]}")

        if analysis:
            latest = analysis[-1]
            score = latest.get("score", 0)
            context_parts.append(f"Qualification score: {score}/10")
            if score < 4.0:
                context_parts.append("Lead scored below threshold — consider FINISH (disqualified)")

        if errors:
            context_parts.append(f"Recent errors: {errors[-1]}")

        approval = state.get("approval_status")
        if approval:
            context_parts.append(f"Approval status: {approval}")

        context = "\n".join(context_parts)

        prompt = [
            SystemMessage(content=self.system_prompt),
            HumanMessage(content=f"Context:\n{context}\n\nWhat is the next routing decision?"),
        ]

        decision = cast(RoutingDecision, await self._structured_model.ainvoke(prompt))

        logger.info(
            "Supervisor routing: %s → %s | reason: %s",
            stage,
            decision.next,
            decision.reasoning,
        )

        # Determine next stage name for state
        stage_map = {
            "researcher": "research",
            "analyzer": "analyze",
            "executor": "propose" if stage in ("analyze", "research") else "execute",
            "human_approval": "approve",
            "FINISH": "done",
        }
        next_stage = stage_map.get(decision.next, stage)

        return {
            "next_agent": decision.next,
            "current_stage": next_stage,
            "messages": [
                AIMessage(
                    content=f"[Supervisor] Routing to {decision.next}: {decision.reasoning}",
                    name="supervisor",
                )
            ],
        }
