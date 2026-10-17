"""Paired, real-execution Skill evaluation against frozen held-out assertions."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from forgeflow.evaluation.artifact_checks import check_artifacts, run_verdict, validate_assertions
from forgeflow.evaluation.golden_registry import (
    get_golden_registry,
    split_train_holdout,
    verify_content_hash,
)
from forgeflow.evaluation.leakage_guard import detect_leakage, experience_source_fingerprints
from forgeflow.repositories import get_experience_repository, get_skill_candidate_repository
from forgeflow.skills.errors import GovernanceError
from forgeflow.skills.evaluator import candidate_spec_hash
from forgeflow.skills.models import SkillEvaluationRecord

_SAFE_TOOLS = frozenset(
    {
        "code.lint",
        "git.diff",
        "textfile.inspect",
        "textfile.edit",
        "document.inspect",
        "document.edit",
        "sheet.inspect",
        "sheet.edit",
        "analysis.profile",
        "artifact.save",
        "report.render",
        "research.search",
        "research.read",
        "research.verify",
        "evidence.read",
        "skills.read",
    }
)


def case_contract(case: Any) -> dict:
    try:
        data = json.loads(case.expected_summary)
    except (ValueError, TypeError) as exc:
        raise GovernanceError("保留案例缺少可执行的 JSON 验收契约", status_code=409) from exc
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("assertions"), list)
        or not 1 <= len(data["assertions"]) <= 30
    ):
        raise GovernanceError("每个保留案例必须包含 1–30 条实际产物断言", status_code=409)
    ids = data.get("resource_ids", [])
    if (
        not isinstance(ids, list)
        or len(ids) > 4
        or any(not isinstance(x, str) or not x for x in ids)
    ):
        raise GovernanceError("保留案例的资源引用无效", status_code=409)
    try:
        validate_assertions(data["assertions"])
    except ValueError as exc:
        raise GovernanceError(str(exc), status_code=409) from exc
    return data


async def preflight_cases(cases: list, ctx: Any) -> dict[str, list[str]]:
    """Authorize and hash real inputs before spending any model budget."""
    from forgeflow.evaluation.leakage_guard import normalize_fingerprint
    from forgeflow.resources.service import get_resource_service

    service = get_resource_service()
    checked = {}
    inputs = {}
    for case in cases:
        contract = case_contract(case)
        ids = contract.get("resource_ids", [])
        if not ids or len(ids) != len(set(ids)):
            raise GovernanceError(
                "正式业务评测需要绑定不重复的文件资源；远程资料请先导入快照", status_code=409
            )
        fingerprints = []
        for resource_id in ids:
            if resource_id not in checked:
                resource = await service.get(ctx.tenant_id, resource_id)
                if (
                    resource is None
                    or resource.tenant_id != ctx.tenant_id
                    or resource.kind != "file"
                ):
                    raise GovernanceError("评测文件不存在或不可访问", status_code=409)
                locator = resource.to_dict()["locator"]
                data = service.blobs.read(locator["storage_ref"])
                digest = hashlib.sha256(data).hexdigest()
                if digest != locator.get("sha256"):
                    raise GovernanceError("评测源文件内容与登记哈希不符", status_code=409)
                checked[resource_id] = digest
            fingerprints.append(checked[resource_id])
        declared = contract.get(
            "resource_fingerprints", [case.source_doc_fingerprint] if len(ids) == 1 else []
        )
        if (
            not isinstance(declared, list)
            or [normalize_fingerprint(value) for value in declared] != fingerprints
            or normalize_fingerprint(case.source_doc_fingerprint) != fingerprints[0]
        ):
            raise GovernanceError("Golden 源指纹与实际评测文件不匹配", status_code=409)
        inputs[case.case_id] = fingerprints
    return inputs


async def evaluation_environment(ctx: Any) -> dict:
    import httpx

    from forgeflow.config import get_settings

    settings = get_settings()
    digest = hashlib.sha256()
    root = Path(__file__).resolve().parents[1]
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    models = {}
    if settings.llm_provider == "ollama":
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            response = await client.get(settings.ollama_base_url.rstrip("/") + "/api/tags")
            response.raise_for_status()
        available = {item["name"]: item.get("digest") for item in response.json()["models"]}
        for name in {settings.ollama_model, settings.ollama_model_strong}:
            version = available.get(name) or available.get(name + ":latest")
            if not version:
                raise GovernanceError("无法固定评测模型版本", status_code=409)
            models[name] = version
    return {
        "runtime_hash": digest.hexdigest(),
        "provider": settings.llm_provider,
        "models": models,
        "num_ctx": settings.ollama_num_ctx,
        "think": settings.ollama_think,
        "actor": ctx.user_id,
        "role": ctx.role,
        "allowed_tools": sorted(ctx.allowed_tools)
        if getattr(ctx, "allowed_tools", None) is not None
        else None,
    }


async def execute_case(case: Any, spec: dict | None, ctx: Any, session_id: str) -> tuple[Any, list]:
    from forgeflow.documents.store import DocArtifactStore
    from forgeflow.repositories.base import new_id
    from forgeflow.runtime.orchestrator import (
        RequestContext,
        TaskCreate,
        get_run_store,
        mark_run_terminal,
        persist_workspace_record,
        run_task,
    )

    contract = case_contract(case)
    context = {"resources": contract.get("resource_ids", []), "session_id": session_id}
    if spec is not None:
        instructions = str(spec.get("instructions") or spec.get("prompt") or "")
        if not 10 <= len(instructions.strip()) <= 12000:
            raise GovernanceError("待测技能指令需要 10–12000 个字符", status_code=409)
        context["instruction_draft"] = {"name": "功能评测固定版本", "instructions": instructions}
    task = TaskCreate(intent=case.instruction, context=context)
    task._functional_eval = True
    task._enable_recovery = True
    runtime = RequestContext(tenant_id=ctx.tenant_id, user_id=ctx.user_id, role=ctx.role)
    declared_tools = spec.get("tools") if spec else None
    allowed = set(_SAFE_TOOLS)
    parent_tools = getattr(ctx, "allowed_tools", None)
    if parent_tools is not None:
        allowed &= set(parent_tools)
    if declared_tools is not None:
        if not isinstance(declared_tools, (list, tuple)) or any(
            not isinstance(tool, str) for tool in declared_tools
        ):
            raise GovernanceError(
                "Skill tools must be an explicit list of tool ids", status_code=409
            )
        allowed &= set(declared_tools)
    runtime.allowed_tools = sorted(allowed)
    run_id = new_id()
    try:
        await run_task(task, runtime, run_id=run_id, register_running=True)
    except (Exception, asyncio.CancelledError) as exc:
        record = get_run_store().get(run_id)
        if (
            record is not None
            and record.tenant_id == ctx.tenant_id
            and record.actor_user_id == ctx.user_id
        ):
            status = "aborted" if isinstance(exc, asyncio.CancelledError) else "failed"
            mark_run_terminal(
                run_id, status=status, outcome=status if status == "aborted" else "failure"
            )
            record.errors.append(str(exc) or "Functional evaluation interrupted")
            await persist_workspace_record(record)
        raise
    record = get_run_store().get(run_id)
    if record is None or record.tenant_id != ctx.tenant_id or record.actor_user_id != ctx.user_id:
        raise GovernanceError("评测执行记录不可读取", status_code=409)
    artifacts = []
    for artifact in record.artifacts:
        if artifact.get("content_ref"):
            artifacts.append(
                (artifact.get("format", "text"), DocArtifactStore().get(artifact["content_ref"]))
            )
        elif artifact.get("content") and (
            artifact.get("kind") != "report_markdown"
            or any(
                assertion["kind"] == "report_answer_contains"
                for assertion in contract["assertions"]
            )
        ):
            artifacts.append((artifact.get("format", "text"), artifact["content"].encode()))
    return record, artifacts


async def evaluate_functionally(
    candidate_id: str,
    dataset_id: str,
    ctx: Any,
    *,
    incumbent_spec: dict | None = None,
    candidate_repo: Any = None,
    golden_repo: Any = None,
    experience_repo: Any = None,
    runner: Any = None,
    job_id: str | None = None,
    expected_spec_hash: str | None = None,
    resume_from: SkillEvaluationRecord | None = None,
) -> SkillEvaluationRecord:
    repo = candidate_repo or get_skill_candidate_repository()
    golden = golden_repo or get_golden_registry()
    experiences = experience_repo or get_experience_repository()
    actual_runner = runner is None
    runner = runner or execute_case
    candidate = await repo.get_candidate(ctx.tenant_id, candidate_id)
    if candidate is None or candidate.status == "archived":
        raise GovernanceError("Candidate not found", status_code=404)
    candidate = copy.deepcopy(candidate)
    original = copy.deepcopy(candidate.draft_spec)
    if original.get("learning_provenance") and (
        not isinstance(original["learning_provenance"], dict) or not candidate.experience_ids
    ):
        raise GovernanceError("学习候选必须有可核对的训练来源", status_code=409)
    digest = candidate_spec_hash(original)
    if expected_spec_hash is not None and expected_spec_hash != digest:
        raise GovernanceError("候选在排队期间发生变化，请重新提交评测", status_code=409)
    dataset = await golden.get_set(ctx.tenant_id, dataset_id)
    if dataset is None or not dataset.frozen or not dataset.initial_gate()["meets_initial_minimum"]:
        raise GovernanceError("需要已冻结、满足最小案例数的独立 Golden 评测集", status_code=409)
    dataset_hash = verify_content_hash(dataset)
    _train, cases = split_train_holdout(dataset.cases)
    if not 1 <= len(cases) <= 24:
        raise GovernanceError(
            "本地功能评测支持 1–24 个保留案例，同源案例不得拆分凑数", status_code=409
        )
    for case in cases:
        case_contract(case)
    training = []
    for experience_id in candidate.experience_ids:
        experience = await experiences.get(ctx.tenant_id, experience_id)
        if experience is None or not experience_source_fingerprints(experience):
            raise GovernanceError(
                "训练来源缺少可核对的源文件指纹，不能证明保留集独立", status_code=409
            )
        training.append(experience)
    if detect_leakage(cases, training).leaked:
        raise GovernanceError("训练数据与保留案例重叠，功能评测被拒绝", status_code=409)
    frozen_inputs = await preflight_cases(cases, ctx) if actual_runner else {}
    training_inputs = {
        fingerprint for item in training for fingerprint in experience_source_fingerprints(item)
    }
    if training_inputs.intersection(value for values in frozen_inputs.values() for value in values):
        raise GovernanceError("实际评测资源与训练来源重叠，评测被拒绝", status_code=409)
    variants = {"without_skill": None}
    if incumbent_spec is not None:
        variants["incumbent"] = copy.deepcopy(incumbent_spec)
    variants["candidate"] = original
    from forgeflow.repositories.base import new_id

    environment = (
        await evaluation_environment(ctx) if actual_runner else {"runner": "offline_fixture"}
    )
    variant_hashes = {
        key: candidate_spec_hash(value) if value is not None else None
        for key, value in variants.items()
    }
    job_id = job_id or new_id()
    rows = []
    if resume_from is not None:
        metrics = resume_from.metrics
        if (
            resume_from.tenant_id != ctx.tenant_id
            or resume_from.target_id != candidate_id
            or resume_from.dataset != dataset_id
            or metrics.get("job_status") not in {"failed", "interrupted", "running"}
            or metrics.get("candidate_spec_hash") != digest
            or metrics.get("dataset_hash") != dataset_hash
            or metrics.get("variant_hashes") != variant_hashes
            or metrics.get("environment") != environment
            or metrics.get("input_fingerprints") != frozen_inputs
        ):
            raise GovernanceError("评测环境、输入或候选已变化，不能复用旧试验", status_code=409)
        rows = copy.deepcopy(metrics.get("cases", []))
        expected = {(case.case_id, variant) for case in cases for variant in variants}
        completed = {(row.get("case_id"), row.get("variant")) for row in rows}
        if (
            len(completed) != len(rows)
            or not completed <= expected
            or any(row.get("trial_status") != "completed" for row in rows)
        ):
            raise GovernanceError("上轮有结果待核实的试验，禁止自动重放", status_code=409)
        job_id = metrics["job_id"]
    completed = {(row["case_id"], row["variant"]) for row in rows}
    last = None

    async def save(verdict: str, status: str, extra: dict | None = None):
        nonlocal last
        metrics = {
            "evaluation_kind": "functional",
            "job_id": job_id,
            "job_status": status,
            "candidate_spec_hash": digest,
            "dataset_hash": dataset_hash,
            "input_fingerprints": frozen_inputs,
            "environment": environment,
            "resumed_from": resume_from.id if resume_from is not None else None,
            "functional_measured": bool(rows) and all(row.get("measured") for row in rows),
            "functional_passed": verdict == "pass",
            "cases": copy.deepcopy(rows),
            "variant_hashes": variant_hashes,
            **(extra or {}),
        }
        last = SkillEvaluationRecord(
            tenant_id=ctx.tenant_id,
            target_id=candidate_id,
            dataset=dataset_id,
            metrics=metrics,
            verdict=verdict,
        )
        await repo.save_evaluation(last)

    await save("pending", "running")
    try:
        async with asyncio.timeout(1800):
            for case in cases:
                for variant, spec in variants.items():
                    if (case.case_id, variant) in completed:
                        continue
                    current = await repo.get_candidate(ctx.tenant_id, candidate_id)
                    if (
                        current is None
                        or candidate_spec_hash(current.draft_spec) != digest
                        or current.status == "archived"
                        or current.experience_ids != candidate.experience_ids
                    ):
                        raise GovernanceError(
                            "候选在评测中发生变化，保留结果但禁止发布", status_code=409
                        )
                    started = time.monotonic()
                    if actual_runner and await preflight_cases([case], ctx) != {
                        case.case_id: frozen_inputs[case.case_id]
                    }:
                        raise GovernanceError("评测资源发生变化，请创建新的评测集", status_code=409)
                    row = {
                        "case_id": case.case_id,
                        "variant": variant,
                        "passed": False,
                        "measured": False,
                        "trial_status": "running",
                    }
                    rows.append(row)
                    await save("pending", "running")
                    try:
                        async with asyncio.timeout(120):
                            record, artifacts = await runner(
                                case, spec, ctx, f"functional-{job_id}-{case.case_id}-{variant}"
                            )
                        checks = check_artifacts(
                            artifacts, case_contract(case)["assertions"], record.tool_invocations
                        )
                        measured = (
                            record.runtime_mode == "react"
                            and record.total_tokens > 0
                            and not (record.llm or {}).get("degraded")
                        )
                        row.update(
                            run_id=record.run_id,
                            status=record.status,
                            outcome=record.outcome,
                            errors=list(record.errors),
                            artifact_hashes=[
                                {"format": fmt, "sha256": hashlib.sha256(data).hexdigest()}
                                for fmt, data in artifacts
                            ],
                            checks=checks,
                            measured=measured,
                            passed=measured and run_verdict(record, checks),
                            false_completion=record.outcome == "success"
                            and not all(checks.values()),
                            total_tokens=record.total_tokens,
                            budget=(record.llm or {}).get("execution_budget"),
                        )
                    except Exception as exc:
                        row["error"] = str(exc)[:300]
                    row["seconds"] = round(time.monotonic() - started, 3)
                    row["trial_status"] = "completed"
                    await save("pending", "running")
    except BaseException as exc:
        await save("pending", "interrupted", {"reason": str(exc)[:300]})
        raise
    candidate_rows = [row for row in rows if row["variant"] == "candidate"]
    passed = len(candidate_rows) == len(cases) and all(row["passed"] for row in candidate_rows)
    passed = passed and all(row["measured"] for row in rows)
    passed = passed and not any(row.get("false_completion") for row in candidate_rows)
    measured_score = sum(row["passed"] for row in candidate_rows) / len(cases)
    benefit = assess_benefit(rows)
    quality_passed = passed
    passed = passed and benefit["positive"]
    if passed and original.get("learning_provenance"):
        updated = copy.deepcopy(candidate)
        updated.draft_spec["learning_provenance"].update(
            functional_evaluation="passed", candidate_id=candidate_id, evaluation_job_id=job_id
        )
        if not await repo.compare_and_save_candidate(updated, original):
            await save("fail", "candidate_changed")
            return last
        digest = candidate_spec_hash(updated.draft_spec)
    await save(
        "pass" if passed else "fail",
        "completed",
        {
            "score": measured_score,
            "quality_passed": quality_passed,
            "benefit": benefit,
            "reason": "" if passed else "未满足全部质量要求或未证明相对基线的额外收益",
        },
    )
    return last


def assess_benefit(rows: list[dict]) -> dict:
    """Require paired non-regression and a quality gain or >=10% token saving."""
    reference = (
        "incumbent" if any(row.get("variant") == "incumbent" for row in rows) else "without_skill"
    )
    base = {row["case_id"]: row for row in rows if row.get("variant") == reference}
    candidate = {row["case_id"]: row for row in rows if row.get("variant") == "candidate"}
    complete = (
        bool(base)
        and base.keys() == candidate.keys()
        and all(
            row.get("measured") and row.get("trial_status") == "completed"
            for row in [*base.values(), *candidate.values()]
        )
    )
    regression = any(
        base[key].get("passed") and not candidate[key].get("passed")
        for key in base.keys() & candidate.keys()
    )
    base_passed = sum(bool(row.get("passed")) for row in base.values())
    candidate_passed = sum(bool(row.get("passed")) for row in candidate.values())
    base_tokens = sum(row.get("total_tokens", 0) for row in base.values())
    candidate_tokens = sum(row.get("total_tokens", 0) for row in candidate.values())
    quality_gain = candidate_passed > base_passed
    token_gain = (
        base_tokens > 0
        and candidate_tokens > 0
        and candidate_tokens <= base_tokens * 0.9
        and candidate_passed == len(candidate)
    )
    return {
        "positive": complete and not regression and (quality_gain or token_gain),
        "reference": reference,
        "quality_gain": quality_gain,
        "token_gain": token_gain,
        "baseline_passed": base_passed,
        "candidate_passed": candidate_passed,
        "baseline_tokens": base_tokens,
        "candidate_tokens": candidate_tokens,
        "minimum_token_saving": 0.1,
    }


async def revise_candidate(
    candidate_id: str, instructions: str, reason: str, tenant: str, *, repo: Any = None
):
    """Create a separate revision linked to real failed feedback, never mutate a release."""
    from forgeflow.skills.models import SkillCandidateRecord

    repo = repo or get_skill_candidate_repository()
    current = await repo.get_candidate(tenant, candidate_id)
    if current is None or current.status == "archived":
        raise GovernanceError("Candidate not found", status_code=404)
    evaluation = await repo.get_evaluation_for(tenant, candidate_id, kind="functional")
    if (
        evaluation is None
        or evaluation.verdict != "fail"
        or evaluation.metrics.get("evaluation_kind") != "functional"
        or evaluation.metrics.get("candidate_spec_hash") != candidate_spec_hash(current.draft_spec)
    ):
        raise GovernanceError("需要当前候选实际失败的功能评测作为修订依据", status_code=409)
    instructions, reason = instructions.strip(), reason.strip()
    if not 10 <= len(instructions) <= 12000 or not 5 <= len(reason) <= 1000:
        raise GovernanceError("修订指令或修改原因长度无效", status_code=422)
    spec = copy.deepcopy(current.draft_spec)
    spec["prompt"] = instructions
    if "instructions" in spec:
        spec["instructions"] = instructions
    learning = spec.get("learning_provenance")
    if isinstance(learning, dict):
        learning["functional_evaluation"] = "unmeasured"
        learning.pop("evaluation_job_id", None)
        learning.pop("candidate_id", None)
    spec["revision"] = {
        "source_candidate_id": candidate_id,
        "source_spec_hash": candidate_spec_hash(current.draft_spec),
        "failed_evaluation_id": evaluation.id,
        "reason": reason,
    }
    revised = SkillCandidateRecord(
        tenant_id=tenant,
        name=current.name[:110] + " · 修订",
        domain=current.domain,
        draft_spec=spec,
        experience_ids=list(current.experience_ids),
    )
    await repo.save_candidate(revised)
    return revised
