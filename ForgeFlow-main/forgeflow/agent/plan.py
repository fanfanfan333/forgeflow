"""INC46 T28 — the Agent Loop's **Plan** value objects.

A plan is what the loop commits to *before* it acts, so a run is auditable and
bounded rather than an improvisation. The spec (task book TABLE 36) pins the shape::

    Plan{plan_id, steps[], assumptions[], success_criteria[], budget}

Design rules
------------
* **plan_id is real and travels.** Every plan carries an id that is written into
  ``run_steps`` (additively — into the existing JSONB columns; **no migration**),
  so a run's trace can be grouped by plan and the "2 次重规划" trajectory is a set
  of *real* ids, never a narrated one.
* **Steps are declared, never inferred.** A step names the tool it drives
  **verbatim**; there is no fuzzy tool selection. The loop still gates every step
  against the eligibility set (``forgeflow.agent.loop``), so a plan can *propose*
  but can never *widen* permission.
* **Risk is derived, not declared.** :func:`risk_level` reuses the single T04
  classifier (``forgeflow.skills.tool_permissions.classify_tool``); a plan is
  "risky" when it contains a ``WRITE`` / ``EXTERNAL`` / ``DANGEROUS`` step.
  Plan-mode approval keys off this (§ loops), never off a hand-set flag.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import uuid4

from forgeflow.agent.budget import Budget

__all__ = [
    "STEP_TOOL",
    "STEP_CLARIFY",
    "STEP_FIX",
    "STEP_REPORT",
    "STEP_TYPES",
    "PlanStep",
    "Plan",
    "new_plan_id",
    "new_plan",
    "plan_signature",
    "risk_level",
    "has_risky_steps",
]

#: A step that drives a platform tool.
STEP_TOOL = "tool"
#: A step that asks a human a question (T21 ``clarify``) before proceeding.
STEP_CLARIFY = "clarify"
#: A step that repairs the previous failure (same tool, corrected args).
STEP_FIX = "fix"
#: The deliverable / summarising step.
STEP_REPORT = "report"
STEP_TYPES: tuple[str, ...] = (STEP_TOOL, STEP_CLARIFY, STEP_FIX, STEP_REPORT)


def new_plan_id() -> str:
    """A globally unique, real plan id (``plan-<uuid4hex>``).

    Not deterministic on purpose: a *real* id is what lets the trace be grouped
    and the replan trajectory be proven. Callers that need a fixed id (tests) pass
    one explicitly via :func:`new_plan`.
    """
    return f"plan-{uuid4().hex}"


@dataclass
class PlanStep:
    """One declared step of a :class:`Plan`.

    Attributes:
        step_id: a per-plan step id (``"<plan_id>:<index>"`` by construction).
        tool: the **platform tool id** this step drives (verbatim; may be empty
            for a ``clarify`` step, which asks a human instead of a tool).
        args: the tool's arguments (fingerprinted, not trusted).
        step_type: one of :data:`STEP_TYPES`.
        purpose: what the step is for (human-readable).
        expected: what a successful observation looks like — the loop's Observe
            phase compares against this to decide "观察与预期不符 ⇒ 重规划".
    """

    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    step_type: str = STEP_TOOL
    purpose: str = ""
    expected: str = ""
    step_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlanStep:
        return cls(
            tool=str(data.get("tool") or ""),
            args=dict(data.get("args") or {}),
            step_type=str(data.get("step_type") or STEP_TOOL),
            purpose=str(data.get("purpose") or ""),
            expected=str(data.get("expected") or ""),
            step_id=str(data.get("step_id") or ""),
        )


@dataclass
class Plan:
    """A bounded plan the Agent Loop commits to and may replan.

    Attributes:
        plan_id: the real plan id (see :func:`new_plan_id`).
        steps: the declared steps, executed in order.
        assumptions: what the plan assumes to be true.
        success_criteria: how the run is judged (Observe + completion).
        budget: the ceilings this plan runs under (:class:`~forgeflow.agent.budget.Budget`).
        reason: why this plan (esp. for a replan — the trigger it answers).
    """

    plan_id: str
    steps: list[PlanStep] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    budget: Budget = field(default_factory=Budget)
    reason: str = ""

    def __post_init__(self) -> None:
        # Stamp a stable, real step id per position (idempotent: an explicit id wins).
        for index, step in enumerate(self.steps):
            if not step.step_id:
                step.step_id = f"{self.plan_id}:{index}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "steps": [s.to_dict() for s in self.steps],
            "assumptions": list(self.assumptions),
            "success_criteria": list(self.success_criteria),
            "budget": self.budget.to_dict(),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Plan:
        budget_raw = data.get("budget")
        budget = Budget(**{k: v for k, v in budget_raw.items() if k in Budget.__dataclass_fields__}) if isinstance(budget_raw, dict) else Budget()
        return cls(
            plan_id=str(data.get("plan_id") or new_plan_id()),
            steps=[PlanStep.from_dict(s) for s in (data.get("steps") or []) if isinstance(s, dict)],
            assumptions=[str(a) for a in (data.get("assumptions") or [])],
            success_criteria=[str(c) for c in (data.get("success_criteria") or [])],
            budget=budget,
            reason=str(data.get("reason") or ""),
        )


def new_plan(
    steps: list[PlanStep | dict[str, Any]],
    *,
    plan_id: str | None = None,
    assumptions: list[str] | None = None,
    success_criteria: list[str] | None = None,
    budget: Budget | None = None,
    reason: str = "",
) -> Plan:
    """Build a :class:`Plan` from steps (dicts or :class:`PlanStep` objects)."""
    normalised = [
        step if isinstance(step, PlanStep) else PlanStep.from_dict(step) for step in steps
    ]
    return Plan(
        plan_id=plan_id or new_plan_id(),
        steps=normalised,
        assumptions=list(assumptions or []),
        success_criteria=list(success_criteria or []),
        budget=budget or Budget(),
        reason=reason,
    )


def plan_signature(plan: Plan) -> str:
    """A stable signature of a plan's *actions* (tool + normalised args), in order.

    Used to tell "the replan produced a genuinely new plan" apart from "the model
    re-emitted the same plan" — the latter must not reset the loop guard.
    """
    from forgeflow.agent.loop_guard import normalize_args

    return "|".join(f"{s.tool}:{normalize_args(s.args)}" for s in plan.steps)


def risk_level(plan: Plan) -> str:
    """The plan's risk tier from its declared tools (reuses the T04 classifier).

    ``DANGEROUS`` ⇒ ``"high"``; any ``WRITE`` / ``EXTERNAL`` ⇒ ``"medium"``; all
    ``READ`` (or no tool steps) ⇒ ``"low"``.
    """
    from forgeflow.skills.tool_permissions import classify_tool, risk_level_from_classes

    classes = [classify_tool(s.tool) for s in plan.steps if s.tool]
    return risk_level_from_classes(classes)


def has_risky_steps(plan: Plan) -> bool:
    """Whether the plan contains a WRITE / EXTERNAL / DANGEROUS step.

    Plan mode (optional) is required for exactly these plans: they must be shown
    to a human and approved through T21 before any step runs.
    """
    return risk_level(plan) in ("medium", "high")
