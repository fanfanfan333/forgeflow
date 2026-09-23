"""Candidate evaluation — jump ④ of the closed loop (docs §2 ④ / P0-04).

Produces a deterministic ``SkillEvaluation`` (no LLM judge needed offline) and
records it so the governance gate can require a passing verdict before
promotion.
"""

from __future__ import annotations

from typing import Any

from forgeflow.config import get_settings
from forgeflow.repositories import get_skill_candidate_repository
from forgeflow.skills.models import SkillEvaluationRecord

# Metric weights for the composite score.
_WEIGHTS = {"completeness": 0.5, "coverage": 0.3, "tool_diversity": 0.2}
_PASS_THRESHOLD = 0.6


def _completeness(draft_spec: dict[str, Any]) -> float:
    required = ("prompt", "steps", "tools", "io_schema")
    present = sum(1 for key in required if draft_spec.get(key))
    return present / len(required)


def _coverage(candidate: Any, required: int) -> float:
    return min(1.0, len(getattr(candidate, "experience_ids", []) or []) / max(1, required))


def _tool_diversity(draft_spec: dict[str, Any]) -> float:
    tools = draft_spec.get("tools") or []
    return min(1.0, len(tools) / 3.0)


async def evaluate_candidate(
    candidate_id: str,
    dataset: str | None = None,
    *,
    tenant_id: str | None = None,
    candidate_repo: Any | None = None,
) -> SkillEvaluationRecord:
    """Evaluate a candidate and persist the verdict.

    Raises ``GovernanceError(404)`` when the candidate does not exist.
    """
    from forgeflow.skills.errors import GovernanceError

    repo = candidate_repo or get_skill_candidate_repository()
    candidate = await repo.get_candidate(tenant_id, candidate_id)
    if candidate is None:
        raise GovernanceError("skill candidate not found", status_code=404)

    settings = get_settings()
    draft_spec = dict(getattr(candidate, "draft_spec", {}) or {})

    metrics = {
        "completeness": round(_completeness(draft_spec), 4),
        "coverage": round(_coverage(candidate, settings.skill_candidate_min_experiences), 4),
        "tool_diversity": round(_tool_diversity(draft_spec), 4),
        "similarity_score": round(float(getattr(candidate, "similarity_score", 0.0)), 4),
    }
    score = sum(metrics[key] * weight for key, weight in _WEIGHTS.items())
    metrics["score"] = round(score, 4)
    verdict = "pass" if score >= _PASS_THRESHOLD else "fail"

    evaluation = SkillEvaluationRecord(
        tenant_id=tenant_id if tenant_id is not None else getattr(candidate, "tenant_id", None),
        target_id=candidate_id,
        dataset=dataset,
        metrics=metrics,
        verdict=verdict,
    )
    await repo.save_evaluation(evaluation)

    # Reflect the verdict on the candidate so the UI can filter on it.
    candidate.status = "evaluating" if verdict == "pass" else "rejected"
    await repo.save_candidate(candidate)
    return evaluation
