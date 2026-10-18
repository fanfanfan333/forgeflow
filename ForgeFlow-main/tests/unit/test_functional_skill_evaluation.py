"""Offline control-flow tests; these fixtures are not real-model evaluation evidence."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from forgeflow.evaluation.golden_registry import GoldenCase, GoldenSet, compute_content_hash
from forgeflow.repositories.memory.skill_repo import MemorySkillCandidateRepository
from forgeflow.skills.errors import GovernanceError
from forgeflow.skills.functional_evaluation import evaluate_functionally
from forgeflow.skills.models import SkillCandidateRecord


def dataset():
    cases = [
        GoldenCase(
            case_id=str(index),
            source_doc_fingerprint="sha256:" + f"{index + 1:064x}",
            instruction="返回实际结果",
            split="holdout" if index < 6 else "train",
            expected_summary=json.dumps({"assertions": [{"kind": "contains", "value": "actual"}]}),
        )
        for index in range(20)
    ]
    result = GoldenSet(tenant_id="functional-tests", cases=cases, frozen=True)
    result.content_hash = compute_content_hash(cases, result.provenance)
    return result


async def setup():
    repo = MemorySkillCandidateRepository()
    candidate = SkillCandidateRecord(
        tenant_id="functional-tests",
        name="test",
        draft_spec={"prompt": "Read the file and return the actual result."},
    )
    await repo.save_candidate(candidate)
    golden = SimpleNamespace(get_set=AsyncMock(return_value=dataset()))
    ctx = SimpleNamespace(tenant_id="functional-tests", user_id="actor", role="manager")
    return repo, candidate, golden, ctx


def record():
    return SimpleNamespace(
        run_id="offline-fixture",
        tenant_id="functional-tests",
        status="completed",
        outcome="success",
        runtime_mode="react",
        total_tokens=10,
        llm={},
        tool_invocations=[],
        errors=[],
    )


@pytest.mark.parametrize(
    "candidate_passed,tokens,positive",
    [(True, 100, False), (True, 95, False), (True, 90, True), (False, 50, False)],
)
def test_benefit_requires_real_gain_without_quality_regression(candidate_passed, tokens, positive):
    from forgeflow.skills.functional_evaluation import assess_benefit

    common = {"case_id": "one", "measured": True, "trial_status": "completed"}
    rows = [
        {**common, "variant": "without_skill", "passed": True, "total_tokens": 100},
        {**common, "variant": "candidate", "passed": candidate_passed, "total_tokens": tokens},
    ]
    assert assess_benefit(rows)["positive"] is positive
    rows[0]["measured"] = False
    assert assess_benefit(rows)["positive"] is False


async def test_correct_results_without_incremental_benefit_do_not_pass():
    repo, candidate, golden, ctx = await setup()

    async def runner(case, spec, context, session_id):
        return record(), [("text", b"actual")]

    result = await evaluate_functionally(
        candidate.id, "dataset", ctx, candidate_repo=repo, golden_repo=golden, runner=runner
    )
    assert result.metrics["quality_passed"] is True
    assert result.metrics["benefit"]["positive"] is False
    assert result.verdict == "fail"


async def test_empty_exception_keeps_root_cause_types_without_inventing_measurement():
    repo, candidate, golden, ctx = await setup()

    async def runner(case, spec, context, session_id):
        try:
            raise AssertionError()
        except AssertionError as exc:
            raise RuntimeError("execution interrupted") from exc

    result = await evaluate_functionally(
        candidate.id, "dataset", ctx, candidate_repo=repo, golden_repo=golden, runner=runner
    )
    assert result.verdict == "fail"
    assert all(row["error_types"] == ["RuntimeError", "AssertionError"] for row in result.metrics["cases"])
    assert not any(row["measured"] for row in result.metrics["cases"])


async def test_functional_evaluation_really_calls_both_variants_and_checks_bytes():
    repo, candidate, golden, ctx = await setup()
    observed = []

    async def runner(case, spec, context, session_id):
        observed.append((case.case_id, spec))
        result = record()
        result.total_tokens = 5 if spec else 10
        return result, [("text", b"actual")]

    evaluation = await evaluate_functionally(
        candidate.id, "dataset", ctx, candidate_repo=repo, golden_repo=golden, runner=runner
    )
    assert len(observed) == 12 and sum(spec is None for _, spec in observed) == 6
    assert evaluation.verdict == "pass"
    assert evaluation.metrics["functional_measured"] is True
    assert evaluation.metrics["score"] == 1


async def test_model_success_without_correct_file_is_not_a_functional_pass():
    repo, candidate, golden, ctx = await setup()

    async def runner(case, spec, context, session_id):
        return record(), [("text", b"actual" if spec is None else b"wrong")]

    evaluation = await evaluate_functionally(
        candidate.id, "dataset", ctx, candidate_repo=repo, golden_repo=golden, runner=runner
    )
    assert evaluation.verdict == "fail"
    assert evaluation.metrics["functional_passed"] is False
    assert all(
        row["false_completion"]
        for row in evaluation.metrics["cases"]
        if row["variant"] == "candidate"
    )


async def test_changed_candidate_is_never_overwritten_by_old_evaluation():
    repo, candidate, golden, ctx = await setup()

    async def runner(case, spec, context, session_id):
        changed = copy.deepcopy(await repo.get_candidate(ctx.tenant_id, candidate.id))
        changed.draft_spec["prompt"] = "new user instructions"
        await repo.save_candidate(changed)
        return record(), [("text", b"actual")]

    with pytest.raises(GovernanceError, match="变化"):
        await evaluate_functionally(
            candidate.id, "dataset", ctx, candidate_repo=repo, golden_repo=golden, runner=runner
        )
    assert (await repo.get_candidate(ctx.tenant_id, candidate.id)).draft_spec[
        "prompt"
    ] == "new user instructions"
    assert (await repo.get_evaluation_for(ctx.tenant_id, candidate.id)).verdict != "pass"


async def test_compare_and_save_does_not_resurrect_archived_candidate():
    repo, candidate, _, ctx = await setup()
    expected = copy.deepcopy(candidate.draft_spec)
    updated = copy.deepcopy(candidate)
    candidate.status = "archived"
    await repo.save_candidate(candidate)
    assert not await repo.compare_and_save_candidate(updated, expected)
    assert (await repo.get_candidate(ctx.tenant_id, candidate.id)).status == "archived"


async def test_publication_rejects_self_asserted_functional_pass(monkeypatch):
    from forgeflow.skills.publish_interlock import _require_learning_evidence

    repo, candidate, _, ctx = await setup()
    monkeypatch.setattr("forgeflow.repositories.get_skill_candidate_repository", lambda: repo)
    candidate.draft_spec["learning_provenance"] = {
        "functional_evaluation": "passed",
        "candidate_id": candidate.id,
        "evaluation_job_id": "invented",
    }
    with pytest.raises(GovernanceError, match="matching measured"):
        await _require_learning_evidence(ctx.tenant_id, candidate.draft_spec)


async def test_functional_admission_is_atomic_and_errors_keep_completed_rows(monkeypatch):
    import asyncio

    from fastapi import HTTPException

    from forgeflow.api.hub_schemas import FunctionalEvaluateRequest
    from forgeflow.api.routers import skills as routes
    from forgeflow.skills.models import SkillEvaluationRecord

    repo, candidate, golden, ctx = await setup()
    gate = asyncio.Event()
    monkeypatch.setattr(routes, "get_skill_candidate_repository", lambda: repo)
    monkeypatch.setattr("forgeflow.evaluation.golden_registry.get_golden_registry", lambda: golden)
    monkeypatch.setattr(routes, "_FUNCTIONAL_JOBS", {})
    monkeypatch.setattr(routes, "_FUNCTIONAL_LIMIT", asyncio.Semaphore(1))
    monkeypatch.setattr(routes, "_FUNCTIONAL_ADMISSION", asyncio.Lock())

    async def fail_after_progress(*args, **kwargs):
        await gate.wait()
        await repo.save_evaluation(
            SkillEvaluationRecord(
                tenant_id=ctx.tenant_id,
                target_id=candidate.id,
                dataset="dataset",
                metrics={
                    "evaluation_kind": "functional",
                    "job_id": kwargs["job_id"],
                    "candidate_spec_hash": kwargs["expected_spec_hash"],
                    "cases": [{"case_id": "real-progress"}],
                },
            )
        )
        raise ValueError("stopped")

    monkeypatch.setattr(
        "forgeflow.skills.functional_evaluation.evaluate_functionally", fail_after_progress
    )
    request = FunctionalEvaluateRequest(dataset="dataset")
    with pytest.raises(HTTPException) as stale:
        await routes.start_functional_evaluation(
            candidate.id,
            FunctionalEvaluateRequest(dataset="dataset", resume_evaluation_id="missing"),
            ctx,
            ctx.tenant_id,
        )
    assert stale.value.status_code == 409
    results = await asyncio.gather(
        *(
            routes.start_functional_evaluation(candidate.id, request, ctx, ctx.tenant_id)
            for _ in range(2)
        ),
        return_exceptions=True,
    )
    assert (
        sum(isinstance(result, HTTPException) and result.status_code == 409 for result in results)
        == 1
    )
    tasks = list(routes._FUNCTIONAL_JOBS.values())
    try:
        gate.set()
        await asyncio.gather(*tasks)
        latest = await repo.get_evaluation_for(ctx.tenant_id, candidate.id)
        assert latest.metrics["cases"] == [{"case_id": "real-progress"}]
        assert latest.metrics["job_status"] == "failed"
        resumed = AsyncMock()
        monkeypatch.setattr("forgeflow.skills.functional_evaluation.evaluate_functionally", resumed)
        queued = await routes.start_functional_evaluation(
            candidate.id,
            FunctionalEvaluateRequest(dataset="dataset", resume_evaluation_id=latest.id),
            ctx,
            ctx.tenant_id,
        )
        await asyncio.gather(*list(routes._FUNCTIONAL_JOBS.values()))
        assert queued.metrics["cases"] == latest.metrics["cases"]
        assert resumed.await_args.kwargs["resume_from"].id == latest.id
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_failed_feedback_revision_preserves_parent_and_does_not_carry_a_pass():
    from forgeflow.skills.functional_evaluation import revise_candidate

    repo, candidate, _, ctx = await setup()
    original = copy.deepcopy(candidate.draft_spec)
    with pytest.raises(GovernanceError, match="实际失败"):
        await revise_candidate(
            candidate.id,
            "Read all cells and preserve formulas.",
            "Fix the demonstrated missing formula check.",
            ctx.tenant_id,
            repo=repo,
        )
    from forgeflow.skills.evaluator import candidate_spec_hash
    from forgeflow.skills.models import SkillEvaluationRecord

    evaluation = SkillEvaluationRecord(
        tenant_id=ctx.tenant_id,
        target_id=candidate.id,
        dataset="frozen",
        verdict="fail",
        metrics={
            "evaluation_kind": "functional",
            "candidate_spec_hash": candidate_spec_hash(original),
        },
    )
    await repo.save_evaluation(evaluation)
    revised = await revise_candidate(
        candidate.id,
        "Read all cells and preserve formulas.",
        "Fix the demonstrated missing formula check.",
        ctx.tenant_id,
        repo=repo,
    )
    assert revised.id != candidate.id and revised.status == "draft"
    assert revised.draft_spec["revision"]["failed_evaluation_id"] == evaluation.id
    assert await repo.get_evaluation_for(ctx.tenant_id, revised.id) is None
    assert (await repo.get_candidate(ctx.tenant_id, candidate.id)).draft_spec == original


@pytest.mark.parametrize(
    "declared,expected", [(["textfile.edit", "textfile.inspect"], ["textfile.inspect"]), ([], [])]
)
async def test_functional_trials_cannot_expand_parent_tool_scope(monkeypatch, declared, expected):
    from forgeflow.skills.functional_evaluation import execute_case

    captured = []
    specs = []

    async def run(task, ctx, **kwargs):
        captured.append(ctx.allowed_tools)
        specs.append(task._functional_skill_spec)

    existing = SimpleNamespace(tenant_id="scope", actor_user_id="actor", artifacts=[])
    monkeypatch.setattr("forgeflow.runtime.orchestrator.run_task", run)
    monkeypatch.setattr(
        "forgeflow.runtime.orchestrator.get_run_store",
        lambda: SimpleNamespace(get=lambda run_id: existing),
    )
    case = SimpleNamespace(
        instruction="Inspect only",
        expected_summary=json.dumps({"assertions": [{"kind": "contains", "value": "actual"}]}),
    )
    ctx = SimpleNamespace(
        tenant_id="scope", user_id="actor", role="manager", allowed_tools={"textfile.inspect"}
    )
    spec = {"prompt": "Inspect the actual file without writing.", "tools": declared,
            "procedure": [{"tool": "textfile.inspect", "purpose": "Read before inspecting"}],
            "package_files": {"references/check.md": "Preserve original numbers."}}
    await execute_case(
        case,
        spec,
        ctx,
        "trial",
    )
    assert captured == [expected]
    assert specs == [spec] and specs[0] is not spec
    spec["package_files"]["references/check.md"] = "changed"
    assert specs[0]["package_files"]["references/check.md"] == "Preserve original numbers."


async def test_evaluation_kinds_keep_latest_failure_and_immutable_history():
    from datetime import timedelta

    from forgeflow.skills.models import SkillEvaluationRecord

    repo = MemorySkillCandidateRepository()
    passed = SkillEvaluationRecord(
        tenant_id="kind-test",
        target_id="candidate",
        verdict="pass",
        metrics={"evaluation_kind": "functional"},
    )
    await repo.save_evaluation(passed)
    passed.verdict = "fail"
    structural = SkillEvaluationRecord(
        tenant_id="kind-test",
        target_id="candidate",
        verdict="pass",
        metrics={"evaluation_kind": "structural"},
        created_at=passed.created_at + timedelta(seconds=1),
    )
    await repo.save_evaluation(structural)
    assert (await repo.get_evaluation_for("kind-test", "candidate")).id == structural.id
    assert (
        await repo.get_evaluation_for("kind-test", "candidate", kind="functional")
    ).verdict == "pass"
    failed = SkillEvaluationRecord(
        tenant_id="kind-test",
        target_id="candidate",
        verdict="fail",
        metrics={"evaluation_kind": "functional"},
        created_at=passed.created_at + timedelta(seconds=2),
    )
    await repo.save_evaluation(failed)
    assert (
        await repo.get_evaluation_for("kind-test", "candidate", kind="functional")
    ).id == failed.id
    assert await repo.get_evaluation_for("other-tenant", "candidate", kind="functional") is None


@pytest.mark.parametrize("cancelled", [False, True])
async def test_functional_trial_exception_finalizes_its_run(monkeypatch, cancelled):
    import asyncio

    from forgeflow.skills.functional_evaluation import execute_case

    existing = SimpleNamespace(tenant_id="scope", actor_user_id="actor", errors=[])
    failure = asyncio.CancelledError() if cancelled else RuntimeError("budget exhausted")

    async def run(*args, **kwargs):
        raise failure

    terminal = []
    persist = AsyncMock()
    monkeypatch.setattr("forgeflow.runtime.orchestrator.run_task", run)
    monkeypatch.setattr(
        "forgeflow.runtime.orchestrator.get_run_store",
        lambda: SimpleNamespace(get=lambda run_id: existing),
    )
    monkeypatch.setattr(
        "forgeflow.runtime.orchestrator.mark_run_terminal",
        lambda run_id, **kwargs: terminal.append(kwargs),
    )
    monkeypatch.setattr("forgeflow.runtime.orchestrator.persist_workspace_record", persist)
    case = SimpleNamespace(
        instruction="Inspect only",
        expected_summary=json.dumps({"assertions": [{"kind": "contains", "value": "actual"}]}),
    )
    ctx = SimpleNamespace(tenant_id="scope", user_id="actor", role="manager")
    with pytest.raises(type(failure)):
        await execute_case(case, None, ctx, "trial")
    assert terminal == [
        {
            "status": "aborted" if cancelled else "failed",
            "outcome": "aborted" if cancelled else "failure",
        }
    ]
    assert existing.errors
    persist.assert_awaited_once_with(existing)


async def test_resume_keeps_completed_failures_and_only_runs_missing_trials():
    from forgeflow.skills.models import SkillEvaluationRecord

    repo, candidate, golden, ctx = await setup()

    async def runner(case, spec, context, session_id):
        result = record()
        return result, [("text", b"actual")]

    done = await evaluate_functionally(
        candidate.id, "dataset", ctx, candidate_repo=repo, golden_repo=golden, runner=runner
    )
    partial = SkillEvaluationRecord(
        tenant_id=ctx.tenant_id,
        target_id=candidate.id,
        dataset="dataset",
        metrics=copy.deepcopy(done.metrics),
    )
    partial.metrics["job_status"] = "interrupted"
    partial.metrics["cases"] = partial.metrics["cases"][:2]
    partial.metrics["cases"][1]["passed"] = False
    observed = []

    async def resume(case, spec, context, session_id):
        observed.append((case.case_id, session_id))
        return await runner(case, spec, context, session_id)

    result = await evaluate_functionally(
        candidate.id,
        "dataset",
        ctx,
        candidate_repo=repo,
        golden_repo=golden,
        runner=resume,
        resume_from=partial,
    )
    skipped = partial.metrics["cases"][0]["case_id"]
    assert len(observed) == 10 and all(case_id != skipped for case_id, _ in observed)
    assert len({session for _, session in observed}) == 10
    assert result.verdict == "fail" and result.metrics["cases"][1]["passed"] is False
    partial.metrics["cases"][0]["trial_status"] = "running"
    with pytest.raises(GovernanceError, match="禁止自动重放"):
        await evaluate_functionally(
            candidate.id,
            "dataset",
            ctx,
            candidate_repo=repo,
            golden_repo=golden,
            runner=resume,
            resume_from=partial,
        )


async def test_preflight_binds_actual_bytes_and_tenant_before_model_use(monkeypatch):
    import hashlib

    from forgeflow.skills.functional_evaluation import preflight_cases

    data = b"actual document"
    digest = hashlib.sha256(data).hexdigest()
    resource = SimpleNamespace(
        tenant_id="scope",
        kind="file",
        to_dict=lambda: {"locator": {"storage_ref": "blob", "sha256": digest}},
    )
    service = SimpleNamespace(
        get=AsyncMock(return_value=resource), blobs=SimpleNamespace(read=lambda ref: data)
    )
    monkeypatch.setattr("forgeflow.resources.service.get_resource_service", lambda: service)
    case = SimpleNamespace(
        case_id="one",
        source_doc_fingerprint="sha256:" + digest,
        expected_summary=json.dumps(
            {"resource_ids": ["resource"], "assertions": [{"kind": "contains", "value": "actual"}]}
        ),
    )
    ctx = SimpleNamespace(tenant_id="scope")
    assert await preflight_cases([case], ctx) == {"one": [digest]}
    case.source_doc_fingerprint = "invented"
    with pytest.raises(GovernanceError, match="指纹"):
        await preflight_cases([case], ctx)
    case.source_doc_fingerprint = digest
    resource.tenant_id = "foreign"
    with pytest.raises(GovernanceError, match="不可访问"):
        await preflight_cases([case], ctx)
