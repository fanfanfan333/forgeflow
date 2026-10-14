from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from forgeflow.skills.learning import verified_run


def receipt():
    return SimpleNamespace(
        status="completed",
        outcome="success",
        llm={
            "acceptance": {
                "complete": True,
                "requirements": [
                    {
                        "status": "satisfied",
                        "evidence": [{"step_id": "edit", "tool": "textfile.edit"}],
                    }
                ],
            }
        },
        tool_invocations=[
            {"step_id": "edit", "tool": "textfile.edit", "status": "ok", "executed": True}
        ],
    )


def test_learning_requires_authoritative_execution_evidence():
    record = receipt()
    assert verified_run(record)
    record.tool_invocations[0]["executed"] = False
    assert not verified_run(record)
    record.tool_invocations[0]["executed"] = True
    record.llm["acceptance"]["requirements"][0]["evidence"][0]["tool"] = "code.execute"
    assert not verified_run(record)


def test_persisted_receipts_survive_header_rehydration():
    record = receipt()
    record.llm["execution_receipts"] = record.tool_invocations
    del record.tool_invocations
    assert verified_run(record)


async def test_structural_evaluation_cannot_publish_a_learned_skill():
    from forgeflow.skills.errors import GovernanceError
    from forgeflow.skills.evaluator import evaluate_candidate
    from forgeflow.skills.governance_gate import promote_candidate
    from forgeflow.skills.models import SkillCandidateRecord

    candidate = SkillCandidateRecord(
        tenant_id="tenant",
        experience_ids=["a", "b", "c"],
        draft_spec={
            "prompt": "clean",
            "steps": ["inspect", "edit"],
            "tools": ["textfile.inspect", "textfile.edit", "report.render"],
            "io_schema": {"input": {}, "output": {}},
            "learning_provenance": {"acceptance_verified": True},
        },
    )
    repo = SimpleNamespace(
        get_candidate=AsyncMock(return_value=candidate),
        save_evaluation=AsyncMock(),
        save_candidate=AsyncMock(),
    )
    evaluation = await evaluate_candidate(candidate.id, tenant_id="tenant", candidate_repo=repo)
    assert evaluation.verdict == "pending"
    assert evaluation.metrics["functional_measured"] is False
    repo.get_evaluation_for = AsyncMock(return_value=evaluation)
    with pytest.raises(GovernanceError, match="promotion blocked"):
        await promote_candidate(
            candidate.id,
            tenant_id="tenant",
            candidate_repo=repo,
            skill_repo=SimpleNamespace(),
            policy_repo=SimpleNamespace(),
            actor="manager",
            actor_role="manager",
        )


async def test_automatic_draft_uses_only_verified_scrubbed_sources(monkeypatch):
    from forgeflow.skills.learning import suggest_from_run
    from forgeflow.skills.models import SkillCandidateRecord

    record = receipt()
    record.run_id, record.tenant_id, record.experience_id = "current", "tenant", "a"
    sources = []
    for index, run_id in enumerate(("current", "verified", "unverified")):
        sources.append(
            SimpleNamespace(
                id=chr(97 + index),
                run_id=run_id,
                outcome="success",
                embedding=[1.0],
                scrub_status="scrubbed",
                scrub_version="1",
                reusable_steps=[{"tool": "textfile.edit"}],
            )
        )
    stored = receipt()
    stored.tenant_id = "tenant"
    exp_repo = SimpleNamespace(
        get=AsyncMock(return_value=sources[0]), list=AsyncMock(return_value=sources)
    )
    cand_repo = SimpleNamespace(save_candidate=AsyncMock())
    workspace = SimpleNamespace(get=AsyncMock(return_value=None))
    monkeypatch.setattr("forgeflow.repositories.get_experience_repository", lambda: exp_repo)
    monkeypatch.setattr("forgeflow.repositories.get_skill_candidate_repository", lambda: cand_repo)
    monkeypatch.setattr("forgeflow.workspace.store.get_workspace_store", lambda: workspace)
    monkeypatch.setattr(
        "forgeflow.runtime.orchestrator.get_run_store",
        lambda: SimpleNamespace(get=lambda run_id: stored if run_id == "verified" else None),
    )
    monkeypatch.setattr(
        "forgeflow.config.get_settings",
        lambda: SimpleNamespace(
            skill_candidate_min_experiences=2, skill_candidate_similarity_threshold=0.85
        ),
    )
    compiler = AsyncMock(return_value=SkillCandidateRecord(tenant_id="tenant", status="draft"))
    monkeypatch.setattr("forgeflow.skills.candidate_compiler.compile_candidate", compiler)
    result = await suggest_from_run(record)
    assert result["status"] == "draft"
    compiler.assert_awaited_once_with(
        "tenant",
        ["a", "b"],
        "manual",
        refine_with_model=False,
        learning_sources=["current", "verified"],
    )
    candidate = cand_repo.save_candidate.call_args.args[0]
    assert candidate.draft_spec["learning_provenance"]["run_ids"] == ["current", "verified"]
    assert candidate.draft_spec["learning_provenance"]["automatic_publish"] is False


async def test_contract_revision_cannot_erase_learning_gate(monkeypatch):
    from forgeflow.skills.engineering import _persist_loop_evaluation

    candidate = SimpleNamespace(
        draft_spec={"learning_provenance": {"run_ids": ["source"]}}, status="draft"
    )
    repo = SimpleNamespace(save_evaluation=AsyncMock(), save_candidate=AsyncMock())
    contract = SimpleNamespace(to_draft_spec=lambda: {"prompt": "revised"})
    evaluation = SimpleNamespace(
        pass_rate=1.0, verified_pass_rate=1.0, sample_size=5, failure_modes=[]
    )
    monkeypatch.setattr("forgeflow.skills.engineering.structural_score", lambda contract: 1.0)
    await _persist_loop_evaluation(
        repo, "tenant", "candidate", contract, evaluation, "pass", candidate=candidate
    )
    assert candidate.draft_spec["learning_provenance"]["run_ids"] == ["source"]
    assert candidate.status == "evaluating"
    assert repo.save_evaluation.call_args.args[0].verdict == "pending"


async def test_pending_approval_cannot_bypass_functional_evaluation():
    from forgeflow.skills.errors import GovernanceError
    from forgeflow.skills.publish_interlock import stage_pending_version

    candidate = SimpleNamespace(
        id="candidate", draft_spec={"learning_provenance": {"functional_evaluation": "unmeasured"}}
    )
    repo = SimpleNamespace(list_versions=AsyncMock())
    with pytest.raises(GovernanceError, match="functional evaluation"):
        await stage_pending_version(
            "tenant",
            SimpleNamespace(id="skill"),
            candidate,
            actor="manager",
            eval_score=1.0,
            skill_repo=repo,
        )
    repo.list_versions.assert_not_awaited()


def test_legacy_empty_failed_and_unverified_runs_are_not_learning_samples():
    record = receipt()
    record.llm = {}
    assert not verified_run(record)
    record = receipt()
    record.llm["acceptance"]["requirements"] = []
    assert not verified_run(record)
    record = receipt()
    record.status = "failed"
    assert not verified_run(record)
    record = receipt()
    record.llm["acceptance"]["requirements"][0]["status"] = "unverified"
    assert not verified_run(record)
