"""Skill Governance Gate — jump ⑤ of the closed loop (docs §2 ⑤ / P0-05).

A candidate can only become a ``SkillVersion`` when it has a *passing*
evaluation **and** the policy engine allows the actor to approve skills.
Anything else fails closed (HTTP 403 / pending approval).
"""

from __future__ import annotations

from typing import Any

from forgeflow.repositories import (
    get_policy_repository,
    get_skill_candidate_repository,
    get_skill_repository,
)
from forgeflow.skills.errors import GovernanceError
from forgeflow.skills.models import SkillRecord, SkillVersionRecord
from forgeflow.skills.versioning import create_version


async def promote_candidate(
    candidate_id: str,
    actor: str,
    *,
    tenant_id: str | None = None,
    candidate_repo: Any | None = None,
    skill_repo: Any | None = None,
    policy_repo: Any | None = None,
    actor_role: str = "manager",
    policy_engine: Any | None = None,
) -> SkillVersionRecord:
    """Promote an evaluated candidate into a versioned Skill.

    Raises:
        GovernanceError(404): candidate missing.
        GovernanceError(403): no passing evaluation, or the policy engine denies.
    """
    cand_repo = candidate_repo or get_skill_candidate_repository()
    sk_repo = skill_repo or get_skill_repository()
    pol_repo = policy_repo or get_policy_repository()

    candidate = await cand_repo.get_candidate(tenant_id, candidate_id)
    if candidate is None:
        raise GovernanceError("skill candidate not found", status_code=404)

    # 1. require a passing evaluation.
    evaluation = await cand_repo.get_evaluation_for(tenant_id, candidate_id)
    if evaluation is None:
        raise GovernanceError("candidate has no evaluation — cannot promote")
    if evaluation.verdict != "pass":
        raise GovernanceError(
            f"evaluation verdict is '{evaluation.verdict}' — promotion blocked"
        )

    # 2. policy gate (RBAC → ABAC → risk → HITL).
    engine = policy_engine
    if engine is None:
        from forgeflow.governance.policy_engine import PolicyEngine

        engine = PolicyEngine(repo=pol_repo)

    decision = await engine.evaluate(
        subject=actor,
        resource="skills",
        action="approve",
        context={"role": actor_role, "candidate_id": candidate_id},
    )
    if not decision.allowed:
        raise GovernanceError(decision.reason or "promotion denied by policy")
    if decision.requires_approval:
        raise GovernanceError(
            "promotion requires human approval (HITL) before it can proceed"
        )

    # 2b. Trust baseline (INC2-13, review finding #8). A passing evaluation says
    # the skill *works*; it says nothing about whether the spec stays inside the
    # tool whitelist, inside the promoter's own permissions, or free of PII.
    # Any failure is a 403 with the concrete reason — never a silent promote.
    from forgeflow.rbac.policies import ROLE_PERMISSIONS
    from forgeflow.skills.trust_baseline import verify_trust_baseline

    actor_permissions = sorted(ROLE_PERMISSIONS.get(actor_role, set()))
    report = verify_trust_baseline(candidate.draft_spec, actor_permissions)
    if not report.ok:
        raise GovernanceError(f"可信基线校验未通过（403）：{report.reason}")

    # 3. create or upgrade the skill + a new version.
    registry_skill = await sk_repo.get_skill_by_name(tenant_id, candidate.name)
    if registry_skill is None:
        registry_skill = SkillRecord(
            tenant_id=tenant_id,
            name=candidate.name,
            domain=candidate.domain,
            owner=actor,
            description=str(candidate.draft_spec.get("prompt", ""))[:280],
            status="published",
            tags=[candidate.domain],
        )
        await sk_repo.create_skill(registry_skill)
        bump = "minor"
        base_version = None
    else:
        bump = "minor"
        base_version = registry_skill.current_version
        registry_skill.owner = registry_skill.owner or actor
        registry_skill.status = "published"

    if base_version is None:
        # First version: seed 0.0.0 then bump minor so the first published
        # version reads 0.1.0.
        registry_skill.current_version = "0.0.0"
        version = await create_version(
            sk_repo,
            tenant_id,
            registry_skill,
            candidate.draft_spec,
            bump="minor",
            actor=actor,
            summary=f"由候选 {candidate_id[:8]} 固化",
            approved_by=actor,
            eval_score=evaluation.metrics.get("score"),
            source_experience_ids=list(candidate.experience_ids),
        )
    else:
        version = await create_version(
            sk_repo,
            tenant_id,
            registry_skill,
            candidate.draft_spec,
            bump=bump,
            actor=actor,
            summary=f"由候选 {candidate_id[:8]} 固化",
            approved_by=actor,
            eval_score=evaluation.metrics.get("score"),
            source_experience_ids=list(candidate.experience_ids),
        )

    candidate.status = "promoted"
    await cand_repo.save_candidate(candidate)
    return version
