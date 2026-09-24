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

#: Audit action label for a release-gate decision (review finding #11).
_RELEASE_AUDIT_ACTION = "skill.release"
#: Audit action label for a canary lifecycle event (INC9 B1) — same single sink
#: (``middleware.audit.write_audit_entry``) as the release gate, so ``/audit``
#: answers for both identically.
_CANARY_AUDIT_ACTION = "skill.canary"


async def _audit_release_decision(
    *,
    tenant_id: str | None,
    actor: str,
    actor_role: str,
    skill_name: str,
    candidate_id: str,
    decision: Any,
    version: str | None,
) -> None:
    """Record one release-gate decision on the platform audit trail.

    Reuses the existing audit sink (``middleware.audit.write_audit_entry`` — the
    PostgreSQL ``audit_log`` table when a pool is available, else the offline
    ring buffer the ``/audit/search`` + ``/audit/export`` API reads). Only the
    *conclusion* plus identifiers are written; the raw baseline metrics and
    per-metric findings are deliberately **not** copied, so the trail stays
    explainable to an auditor without leaking evaluation internals.

    Best-effort: the sink swallows its own failures, so a broken audit can never
    block a promotion.
    """
    from forgeflow.middleware.audit import write_audit_entry

    await write_audit_entry(
        {
            "user_id": actor,
            "role": actor_role,
            "action": _RELEASE_AUDIT_ACTION,
            "resource": "skills",
            "resource_id": skill_name,
            "outcome": "allowed" if decision.allowed else "denied",
            "workspace_id": tenant_id,
            "metadata": {
                "skill": skill_name,
                "candidate_id": candidate_id,
                "version": version,
                "severity": decision.severity,
                "allowed": decision.allowed,
                "reason": decision.reason,
            },
        }
    )


async def _audit_canary_event(
    *,
    tenant_id: str | None,
    actor: str,
    actor_role: str,
    skill_name: str,
    version: str | None,
    action: str,
    severity: str,
    outcome: str,
    reason: str,
) -> None:
    """Record one canary lifecycle event on the **existing** audit sink.

    Reuses ``middleware.audit.write_audit_entry`` — the same nine-field sink the
    release gate and ``/audit/search`` use — so no new audit component and no new
    table is introduced (INC9 §2.1.4 (d)/(e)). Only the conclusion plus
    identifiers are written; no evaluation internals leak. Best-effort: the sink
    swallows its own failures, so a broken audit can never block a release.
    """
    from forgeflow.middleware.audit import write_audit_entry

    await write_audit_entry(
        {
            "user_id": actor,
            "role": actor_role,
            "action": _CANARY_AUDIT_ACTION,
            "resource": "skills",
            "resource_id": skill_name,
            "outcome": outcome,
            "workspace_id": tenant_id,
            "metadata": {
                "skill": skill_name,
                "version": version,
                "action": action,
                "severity": severity,
                "reason": reason,
            },
        }
    )


def _pick_canary_version(versions: list[Any], current_version: str | None) -> Any | None:
    """The version currently under canary exposure, if any.

    Prefers a version explicitly marked ``release_state == "canary"``. Falls back
    to the newest version that is neither the incumbent nor already rolled back —
    which covers the PostgreSQL backend, where ``release_state`` is a model-only
    field (INC9 is zero-migration) and therefore reads back as the default.
    """
    for version in versions:
        if getattr(version, "release_state", "promoted") == "canary":
            return version
    for version in versions:
        state = getattr(version, "release_state", "promoted")
        if version.semver != (current_version or "") and state != "rolled_back":
            return version
    return None


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

    # 2c. Release gate — Skill CI/CD (architecture review #2/#6). A *passing*
    # evaluation is an absolute bar, so an upgrade that scores just above the
    # threshold would still supersede a better version. Compare the candidate's
    # recorded metrics against the version being superseded and 403 a regression.
    # Only *recorded* metrics are compared (no eval suite at promote time) and a
    # first release has no baseline, so it is reported as "no_baseline" — never
    # silently as a clean pass.
    #
    # Every decision is also *recorded* on the audit trail (review finding #11):
    # without it a caller cannot tell "released for lack of a baseline" from
    # "verified clean", and cannot answer after the fact why a version shipped.
    from forgeflow.config import get_settings
    from forgeflow.skills.release_gate import baseline_from_version, evaluate_release

    registry_skill = await sk_repo.get_skill_by_name(tenant_id, candidate.name)
    release = None
    superseded_version: str | None = None
    if get_settings().skill_release_gate_enabled:
        superseded = None
        if registry_skill is not None:
            superseded_version = registry_skill.current_version
            superseded = await sk_repo.get_version(
                tenant_id, registry_skill.id, registry_skill.current_version or ""
            )
        release = evaluate_release(evaluation.metrics, baseline_from_version(superseded))
        if not release.allowed:
            # Blocked (regression): record the verdict, then fail closed. The
            # version that *stays* is named so the trail says which release the
            # 403 applied to.
            await _audit_release_decision(
                tenant_id=tenant_id,
                actor=actor,
                actor_role=actor_role,
                skill_name=candidate.name,
                candidate_id=candidate_id,
                decision=release,
                version=superseded_version,
            )
            raise GovernanceError(f"Skill 发布门禁未通过（403）：{release.reason}")

    # 3. create or upgrade the skill + a new version.
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

    # INC9 B1 — canary release. With canary disabled (the default) this is a
    # no-op: ``publish=True`` reproduces the historical all-at-once switch-over
    # byte-for-byte. With canary enabled the new version is recorded
    # (``publish=False``) but the incumbent stays current until an A/B verdict
    # resolves it (``resolve_canary``). A *first* release has no incumbent to
    # compare against, so it is always published at once.
    canary = bool(getattr(get_settings(), "skill_canary_enabled", False)) and (
        base_version is not None
    )
    publish = not canary

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
            publish=publish,
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
            publish=publish,
        )

    if canary:
        # The version is under exposure but is NOT the default yet.
        version.release_state = "canary"
        await _audit_canary_event(
            tenant_id=tenant_id,
            actor=actor,
            actor_role=actor_role,
            skill_name=candidate.name,
            version=version.semver,
            action="canary_start",
            severity="canary_start",
            outcome="allowed",
            reason="已发布为灰度候选版本，current_version 保持旧版",
        )

    # Record the *allowed* release decision (no_baseline / ok / warning) together
    # with the freshly minted version, so the audit trail can distinguish a
    # baseline-less pass from a verified-clean one and name the shipped version.
    if release is not None:
        await _audit_release_decision(
            tenant_id=tenant_id,
            actor=actor,
            actor_role=actor_role,
            skill_name=candidate.name,
            candidate_id=candidate_id,
            decision=release,
            version=version.semver,
        )

    candidate.status = "promoted"
    await cand_repo.save_candidate(candidate)
    return version


async def resolve_canary(
    skill_id: str,
    *,
    tenant_id: str | None = None,
    actor: str = "system",
    actor_role: str = "manager",
    canary_metrics: dict[str, Any] | None = None,
    incumbent_metrics: dict[str, Any] | None = None,
    sample_n: int = 0,
    skill_repo: Any | None = None,
) -> dict[str, Any]:
    """Resolve a skill's canary window with a same-yardstick A/B decision.

    Runs :func:`forgeflow.skills.canary.decide_ab` over the two versions'
    recorded metrics (the canary's evaluation metrics vs the incumbent's stored
    ``eval_score``), then applies the outcome:

    * ``promote``  → repoint ``current_version`` at the canary version;
    * ``rollback`` → keep the incumbent, mark the canary ``rolled_back``;
    * ``hold``     → change nothing (too few samples / no comparable baseline).

    Every outcome is recorded via the **existing** audit sink
    (``action="skill.canary"``), reusing ``_audit_canary_event`` — no new
    component, no new table (INC9 §2.1.4 (e)).

    Raises:
        GovernanceError(404): the skill does not exist for this tenant.
    """
    from forgeflow.config import get_settings
    from forgeflow.skills.canary import decide_ab

    sk_repo = skill_repo or get_skill_repository()
    skill = await sk_repo.get_skill(tenant_id, skill_id)
    if skill is None:
        raise GovernanceError("skill not found", status_code=404)

    versions = await sk_repo.list_versions(tenant_id, skill_id)
    canary_version = _pick_canary_version(versions, skill.current_version)

    decision = decide_ab(
        canary_metrics,
        incumbent_metrics,
        sample_n=sample_n,
        min_samples=int(getattr(get_settings(), "skill_canary_min_samples", 10) or 10),
    )

    applied = decision.action
    if decision.action == "promote" and canary_version is not None:
        skill.current_version = canary_version.semver
        await sk_repo.update_skill(skill)
    elif decision.action == "rollback" and canary_version is not None:
        # Keep the incumbent as current_version; mark the canary as rolled back.
        canary_version.release_state = "rolled_back"
    else:
        # hold, or a decision we cannot apply because there is no canary version
        # to act on — surfaced honestly rather than silently doing nothing.
        if canary_version is None:
            applied = "hold"

    outcome = {
        "promote": "allowed",
        "rollback": "denied",
    }.get(applied, "allowed")

    await _audit_canary_event(
        tenant_id=tenant_id,
        actor=actor,
        actor_role=actor_role,
        skill_name=skill.name,
        version=canary_version.semver if canary_version is not None else None,
        action=applied,
        severity=decision.severity,
        outcome=outcome,
        reason=decision.reason,
    )

    return {
        "skill_id": skill_id,
        "action": applied,
        "severity": decision.severity,
        "reason": decision.reason,
        "sample_n": decision.sample_n,
        "baseline_present": decision.baseline_present,
        "current_version": skill.current_version,
        "canary_version": canary_version.semver if canary_version is not None else None,
    }
