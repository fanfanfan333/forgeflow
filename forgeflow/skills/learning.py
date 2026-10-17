"""Suggest candidates only from authoritative, accepted execution receipts."""

from __future__ import annotations

from typing import Any


def verified_run(record: Any) -> bool:
    if record is None or record.status != "completed" or record.outcome != "success":
        return False
    if (record.llm or {}).get("evaluation_only"):
        return False
    acceptance = (record.llm or {}).get("acceptance") or {}
    rows = acceptance.get("requirements") or []
    if acceptance.get("complete") is not True or not rows:
        return False
    actual = {
        (r.get("step_id"), r.get("tool"))
        for r in getattr(
            record, "tool_invocations", (record.llm or {}).get("execution_receipts", [])
        )
        if r.get("step_id") and r.get("status") == "ok" and r.get("executed") is True
    }
    return all(
        row.get("status") == "satisfied"
        and row.get("evidence")
        and all((e.get("step_id"), e.get("tool")) in actual for e in row["evidence"])
        for row in rows
    )


async def suggest_from_run(record: Any) -> dict[str, Any]:
    from forgeflow.config import get_settings
    from forgeflow.experience.embedding import cosine_similarity, embed_text
    from forgeflow.privacy.scrubber import filter_miner_eligible
    from forgeflow.repositories import get_experience_repository, get_skill_candidate_repository
    from forgeflow.runtime.orchestrator import get_run_store
    from forgeflow.skills.candidate_compiler import compile_candidate
    from forgeflow.workspace.store import get_workspace_store

    if not verified_run(record) or not record.tenant_id:
        return {"status": "ineligible", "reason": "Task acceptance is not verified"}
    repo = get_experience_repository()
    seed = await repo.get(record.tenant_id, record.experience_id)
    if seed is None or not filter_miner_eligible([seed]) or not seed.reusable_steps:
        return {"status": "ineligible", "reason": "No scrubbed executable experience"}
    if seed.run_id != record.run_id:
        original = await get_workspace_store().get(record.tenant_id, seed.run_id)
        if not verified_run(original):
            return {"status": "ineligible", "reason": "Merged experience has no verified source"}
    settings = get_settings()
    seed_embedding = seed.embedding or embed_text(f"{seed.summary} {' '.join(seed.tags)}")
    selected = [seed]
    for experience in filter_miner_eligible(await repo.list(record.tenant_id, limit=200)):
        if (
            experience.id == seed.id
            or experience.outcome != "success"
            or not experience.reusable_steps
        ):
            continue
        if [step.get("tool") for step in experience.reusable_steps] != [
            step.get("tool") for step in seed.reusable_steps
        ]:
            continue
        if (
            cosine_similarity(
                seed_embedding,
                experience.embedding
                or embed_text(f"{experience.summary} {' '.join(experience.tags)}"),
            )
            < settings.skill_candidate_similarity_threshold
        ):
            continue
        source = get_run_store().get(experience.run_id)
        if source is None or source.tenant_id != record.tenant_id:
            source = await get_workspace_store().get(record.tenant_id, experience.run_id)
        if verified_run(source):
            selected.append(experience)
    if len(selected) < settings.skill_candidate_min_experiences:
        return {"status": "insufficient", "verified_sources": len(selected)}
    candidates = get_skill_candidate_repository()
    candidate = await compile_candidate(
        record.tenant_id,
        [e.id for e in selected],
        "manual",
        refine_with_model=False,
        learning_sources=[e.run_id for e in selected],
    )
    if candidate.status == "insufficient":
        return {"status": "insufficient"}
    candidate.draft_spec.setdefault(
        "learning_provenance",
        {
            "run_ids": [e.run_id for e in selected],
            "acceptance_verified": True,
            "functional_evaluation": "unmeasured",
            "automatic_publish": False,
        },
    )
    await candidates.save_candidate(candidate)
    return {"status": "draft", "candidate_id": candidate.id, "verified_sources": len(selected)}
