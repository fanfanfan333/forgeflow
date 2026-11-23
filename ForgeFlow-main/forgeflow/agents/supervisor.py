"""SupervisorAgent — routes workflow to the correct worker using structured LLM output.

The supervisor never calls tools directly. It reads the current stage + message
history and emits a RoutingDecision that the conditional edge uses to pick the
next node. This keeps routing logic deterministic and auditable.
"""

from __future__ import annotations

import logging
from typing import Literal, cast

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel

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
Always include a brief reasoning for your routing decision."""

WORKERS = Literal["researcher", "analyzer", "executor", "human_approval", "FINISH"]


class RoutingDecision(BaseModel):
    next: WORKERS
    reasoning: str


#: The one worker each documented stage is allowed to hand off to
#: (``SUPERVISOR_SYSTEM`` stages 1-6). This table — not the model's output — is
#: the stage contract; the model may only choose among the legal exits.
_STAGE_WORKER: dict[str, str] = {
    "qualify": "researcher",
    "research": "analyzer",
    "analyze": "executor",
    "propose": "human_approval",
    "approve": "executor",
    "done": "FINISH",
    "failed": "FINISH",
}

#: Stages whose FINISH is a *legal* exit only under extra conditions, so a bare
#: model "FINISH" is not enough (BIZ-03).
_SCORE_THRESHOLD = 4.0


def _latest_qualification_score(state: WorkflowState) -> float | None:
    """The newest analysis score, or ``None`` when no scoring has happened."""
    scores = state.get("analysis_scores") or []
    if not scores:
        return None
    score = (scores[-1] or {}).get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return None
    return float(score)


def workflow_is_complete(stage: str, state: WorkflowState) -> bool:
    """Whether the workflow is genuinely at an end condition (BIZ-03).

    The supervisor used to treat a model ``FINISH`` as proof of completion and
    wrote ``current_stage = "done"`` unconditionally, so a model misjudgement —
    or a prompt-injected one — could end a run before research, analysis or
    approval happened, and the API reported it as completed. Completion is a
    program fact, not a model opinion:

    * ``done`` / ``failed`` — already terminal;
    * ``analyze`` — only a **disqualifying** score (< 4.0) may finish early;
    * ``approve`` — a **rejected** approval is a legitimate terminal outcome.

    Every other stage must keep moving through the documented workers.
    """
    if stage in ("done", "failed"):
        return True
    approval = state.get("approval_status")
    if stage == "approve" and approval == "rejected":
        return True
    if stage == "analyze":
        score = _latest_qualification_score(state)
        return score is not None and score < _SCORE_THRESHOLD
    return False


def gate_routing(
    wanted: str, stage: str, state: WorkflowState
) -> tuple[str, str | None]:
    """Apply the stage contract to the model's routing decision (BIZ-01/BIZ-03).

    Returns ``(worker, override_reason)``. ``override_reason`` is ``None`` when
    the model agreed with the contract, so the audit log only records genuine
    corrections. Rules:

    * ``approve`` with a **pending** approval can never route to ``executor``:
      the executor's execute branch is gated on approval, and re-drafting is the
      only honest action until a human decides (BIZ-01).
    * ``FINISH`` is only accepted where :func:`workflow_is_complete` says the
      workflow really ended (BIZ-03).
    * every other stage may only go to its documented worker.
    """
    if stage not in _STAGE_WORKER:
        # Unknown stage (a custom graph): do not invent a contract for it.
        return wanted, None

    if stage == "approve":
        approval = state.get("approval_status")
        if approval == "approved":
            return "executor", "execution_required" if wanted == "FINISH" else None
        if wanted == "executor":
            return "human_approval", "approval_pending"
        return "human_approval", None

    expected = _STAGE_WORKER[stage]
    if wanted == "FINISH":
        if workflow_is_complete(stage, state):
            return "FINISH", None
        return expected, "finish_rejected_incomplete"
    if wanted == expected:
        return wanted, None
    return expected, "stage_contract"


def stage_after(worker: str, stage: str, state: WorkflowState) -> str:
    """The stage name that follows a worker, derived from the *gated* worker.

    ``executor`` means "execute" only in the one situation where execution is
    authorised — the ``approve`` stage with an approved request. Anywhere else
    it means "draft a proposal", so a pending approval can never be turned into
    an execution by a routing slip (BIZ-01).
    """
    if worker == "executor":
        approved = (
            stage == "approve" and state.get("approval_status") == "approved"
        )
        return "execute" if approved else "propose"
    if worker == "FINISH":
        return "failed" if stage == "failed" else "done"
    return {
        "researcher": "research",
        "analyzer": "analyze",
        "human_approval": "approve",
    }.get(worker, stage)


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

        # Build context summary for routing
        context_parts = [f"Current stage: {stage}"]

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

        # The model proposed a route; the documented stage contract decides. The
        # gate is what makes "approval pending" un-executable and "FINISH"
        # conditional on real completion (BIZ-01 / BIZ-03), regardless of what a
        # misjudged or injected model answer says.
        next_agent, override = gate_routing(decision.next, stage, state)
        next_stage = stage_after(next_agent, stage, state)
        if override is not None:
            logger.warning(
                "Supervisor routing overridden at stage %s: model=%s → %s (%s)",
                stage,
                decision.next,
                next_agent,
                override,
            )

        note = (
            f" [override={override}: model proposed {decision.next}]"
            if override is not None
            else ""
        )
        return {
            "next_agent": next_agent,
            "current_stage": next_stage,
            "messages": [
                AIMessage(
                    content=(
                        f"[Supervisor] Routing to {next_agent}: "
                        f"{decision.reasoning}{note}"
                    ),
                    name="supervisor",
                )
            ],
        }
