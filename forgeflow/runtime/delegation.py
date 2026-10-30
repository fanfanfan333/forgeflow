"""Read-only specialists reuse the ReAct loop and the parent's execution budget."""

from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from forgeflow.runtime.execution_budget import current_budget

ROLE_TOOLS = {
    "research": {
        "research.search",
        "research.read",
        "research.verify",
        "evidence.read",
        "skills.read",
    },
    "document_review": {
        "document.inspect",
        "sheet.inspect",
        "textfile.inspect",
        "pdf.inspect",
        "evidence.read",
        "skills.read",
    },
    "code_review": {"textfile.inspect", "git.diff", "code.lint", "evidence.read", "skills.read"},
}


def _parent_context(ctx: Any) -> dict[str, Any]:
    parent_task = getattr(ctx, "task_context", None)
    context = getattr(parent_task, "context", None)
    return context if isinstance(context, dict) else {}


def _allowed_resource_ids(ctx: Any) -> set[str]:
    """Resource ids the parent legitimately holds (declared + scoped)."""
    allowed = {
        str(r)
        for r in (_parent_context(ctx).get("resources") or [])
        if isinstance(r, str) and r.strip()
    }
    try:
        from forgeflow.runtime.input_scope import current_input_scope

        scope = current_input_scope.get()
        if scope is not None:
            allowed.update(str(k) for k in (scope.resources or {}))
    except Exception:  # noqa: BLE001 — no scope ⇒ declared ids only
        pass
    return allowed


def _filter_resources(resources: Any, ctx: Any) -> tuple[list[str], list[str]]:
    """Split a brief's resources into kept ids and dropped entries.

    2026-10-30 handoff retest (B run ff727731): the model called
    ``agent.delegate`` three times, every call failing with
    "Delegation resources must be selected parent resources", so B never
    reached ``code.execute`` and started zero containers. The old check
    only accepted ids already in the parent's declared ``resources``
    list — but a handoff/code parent's real inputs (a ``repo_path``, a
    server-derived handoff document, a continued parent's files) are not
    declared resource ids, and the model cannot know the ids anyway: the
    tool schema gives it no list of them. Dropping unknown entries is
    fail-closed (the child never gains access through a dropped entry)
    while letting the delegation run on the inputs it *does* inherit
    (see :func:`_child_inputs`); raising for the whole brief killed the
    run for an argument-shape mistake.
    """
    if not isinstance(resources, list) or any(not isinstance(r, str) for r in resources):
        raise ValueError("Delegation resources must be a list of strings")
    allowed = _allowed_resource_ids(ctx)
    kept = [r for r in resources if r in allowed]
    dropped = [r for r in resources if r not in allowed]
    return kept, dropped


_ROLE_SYNONYMS = {
    "researcher": "research",
    "document_reviewer": "document_review",
    "doc_review": "document_review",
    "documentreview": "document_review",
    "code_reviewer": "code_review",
    "codereview": "code_review",
}


def _normalize_role(role: str) -> str:
    """Map the names a model naturally uses onto the three real roles."""
    key = str(role or "").strip().lower().replace(" ", "_").replace("-", "_")
    return _ROLE_SYNONYMS.get(key, key)


def _filter_requirement_ids(ids: Any, ctx: Any) -> tuple[list[str], list[str]]:
    """Split requirement ids into known and dropped (same rule as resources).

    Requirement ids are server-generated provenance keys (``edit``,
    ``return``, ``research`` …) that the model never sees as a list in
    its tool schema — it can only guess them. One guessed id used to
    raise "Unknown parent requirement" for the whole delegation, and the
    model retried the identical call until budget_exceeded (B run
    6ecb399c at 0199b7d). Dropping unknown ids is safe: they are only
    provenance on the delegation record, never an access grant — the
    child generates its own acceptance requirements from its instruction.
    """
    if not isinstance(ids, list) or any(not isinstance(r, str) for r in ids):
        raise ValueError("Requirement ids must be a list of strings")
    parent_task = getattr(ctx, "task_context", None)
    context = getattr(parent_task, "context", None) if parent_task is not None else {}
    known = {
        str(r.get("id"))
        for r in (context or {}).get("llm_runtime", {}).get("acceptance", {}).get("requirements", [])
        if isinstance(r, dict) and r.get("id")
    }
    kept = [r for r in ids if r in known]
    dropped = [r for r in ids if r not in known]
    return kept, dropped


def _str_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v or "").strip()]
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def _child_inputs(ctx: Any) -> dict[str, Any]:
    """The parent's real, already-authorized inputs for a specialist child.

    A child created with only ``{"resources": [...]}`` could inspect
    nothing when the parent's files arrived via ``repo_path``, a
    server-derived handoff, or a continued parent — exactly the B-run
    situation: the parent had a repo and an A DOCX, neither a declared
    resource id, so a specialist child had no platform-provided path and
    its ``textfile.inspect`` / ``document.inspect`` calls blocked with
    "missing readable file path". Everything here is already
    parent-authorized: scope paths are tenant-checked, the repo path is
    the parent's own declaration, and handoff documents are re-derived
    through the tenant/user-checked source-run lookup (any failure
    degrades to "no handoff paths", never to a guessed document).
    """
    parent_ctx = _parent_context(ctx)
    out: dict[str, Any] = {}
    try:
        from forgeflow.runtime.input_scope import current_input_scope

        scope = current_input_scope.get()
        inherited = dict(scope.inherited_inputs) if scope is not None else {}
    except Exception:  # noqa: BLE001 — no scope ⇒ parent context only
        inherited = {}
    document_paths = _str_list(inherited.get("document_paths")) + _str_list(
        parent_ctx.get("document_paths")
    )
    document_names = _str_list(inherited.get("document_names")) + _str_list(
        parent_ctx.get("document_names")
    )
    text_paths = _str_list(inherited.get("text_paths")) + _str_list(parent_ctx.get("text_paths"))
    text_names = _str_list(inherited.get("text_names")) + _str_list(parent_ctx.get("text_names"))
    # Handoff documents: the parent's server-derived handoff, resolved to
    # real DocArtifactStore paths the child can document.inspect. Shared
    # with orchestrator._document_args via _handoff_document_paths, so
    # the two can never disagree about which documents a handoff parent
    # holds. Any failure degrades to no handoff paths.
    handoff_docs: list[str] = []
    handoff_titles: list[str] = []
    try:
        from forgeflow.runtime.orchestrator import _handoff_document_paths

        handoff_docs, handoff_titles = _handoff_document_paths(
            parent_ctx, tenant_id=getattr(ctx, "tenant_id", None), user_id=getattr(ctx, "user_id", None)
        )
    except Exception:  # noqa: BLE001 — handoff lookup failure ⇒ no handoff paths
        handoff_docs = []
        handoff_titles = []
    document_paths.extend(handoff_docs)
    document_names.extend(handoff_titles)
    # Repo files: top-level text/code files of the parent's own repo, so a
    # code_review child can actually file_index them. Shared with
    # orchestrator._textfile_args via _repo_text_files.
    repo_files: list[str] = []
    repo_path = str(parent_ctx.get("repo_path") or "").strip()
    if repo_path:
        try:
            from forgeflow.runtime.orchestrator import _repo_text_files

            repo_files = _repo_text_files(repo_path)
        except Exception:  # noqa: BLE001 — unreadable repo ⇒ no repo files
            repo_files = []
    text_paths.extend(repo_files)
    text_names.extend([p.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] for p in repo_files])
    all_paths: list[str] = []
    for candidate in (
        document_paths + text_paths + _str_list(inherited.get("paths")) + _str_list(parent_ctx.get("paths"))
    ):
        if candidate not in all_paths:
            all_paths.append(candidate)
    if document_paths:
        out["document_paths"] = document_paths
        out["document_names"] = document_names
    if text_paths:
        out["text_paths"] = text_paths
        out["text_names"] = text_names
    if all_paths:
        out["paths"] = all_paths
    if repo_path:
        out["repo_path"] = repo_path
    return out


def _validate_delegation_item(item: Any, ctx: Any) -> dict[str, Any]:
    """Validate one specialist brief without starting it.

    Mirrors the pre-flight checks in :func:`delegate` (role, bounded
    instruction, parent requirements, no nesting) so a parallel batch
    fails *before* any child starts, instead of leaving a half-started
    fan-out when the second brief is invalid. Unknown resources are
    *dropped*, not raised (see :func:`_filter_resources`): the child
    still runs on its inherited inputs, and the dropped entries are
    reported in the result.
    """
    parent, parent_task = ctx.runtime_context, ctx.task_context
    if parent is None or parent_task is None or getattr(parent, "delegation_role", None):
        raise PermissionError("Nested or detached delegation is not allowed")
    if not isinstance(item, dict):
        raise ValueError("Each delegation must be an object")
    role = _normalize_role(str(item.get("role") or ""))
    if role not in ROLE_TOOLS:
        raise ValueError(f"Unknown specialist role; valid roles are: {', '.join(sorted(ROLE_TOOLS))}")
    instruction = item.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 8000:
        raise ValueError("A bounded specialist instruction is required")
    kept, dropped = _filter_resources(item.get("resources", []), ctx)
    req_kept, req_dropped = _filter_requirement_ids(item.get("requirement_ids", []), ctx)
    return {
        "role": role,
        "instruction": instruction,
        "resources": kept,
        "resources_dropped": dropped,
        "requirement_ids": req_kept,
        "requirement_ids_dropped": req_dropped,
    }


async def delegate_parallel(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Run 1-4 read-only specialists concurrently (fan-out / fan-in).

    The supervisor graph and the single :func:`delegate` tool are both
    sequential: a multi-domain review paid the sum of every specialist's
    latency. CrewAI's parallel process and LangGraph's fan-out pattern are
    the open-source reference here — independent specialists should run at
    the same time and be merged afterwards, not queued.

    Every child still goes through :func:`delegate` unchanged: same role
    tool allow-list, same parent-resource / requirement checks, same
    server-owned budget (the budget caps delegations at 4), and no nested
    delegation. One child's failure is isolated — it is reported in
    ``results`` with ``ok: False`` and does not cancel the others — and
    the merged return never claims a failed child succeeded.

    Failure shape matters for the model loop (2026-10-30 handoff retest):
    the old code raised for the *whole batch* when the model passed a
    single brief in list form, or one invalid brief among valid ones, or
    a batch larger than the remaining budget. The model then retried the
    identical call until the run hit its model-call ceiling, with no
    progress. Now a single brief runs as a 1-item batch, an invalid brief
    is reported as that item's failure (valid siblings still run), and
    briefs beyond the 1-4 / budget ceiling are reported as skipped items
    instead of aborting the batch. An empty list is still a hard error:
    there is nothing to run. A missing budget or a nested/detached
    context is still a hard error, and a nested/detached context fails
    every item without starting a child — those are security boundaries,
    not model-argument mistakes, and no child ever runs under them.
    """
    items = args.get("delegations")
    if not isinstance(items, list) or not items:
        raise ValueError("Parallel delegation requires at least 1 delegation brief")
    budget = current_budget.get()
    if budget is None:
        raise PermissionError("Delegation requires the parent's server-owned budget")
    # One result slot per input brief, in batch order. Valid briefs are
    # collected for a concurrent gather; invalid / over-cap briefs fill
    # their slot immediately and never start a child.
    results: list[dict[str, Any] | None] = [None] * len(items)
    to_run: list[tuple[int, dict[str, Any]]] = []
    for idx, item in enumerate(items):
        role = item.get("role") if isinstance(item, dict) else ""
        try:
            brief = _validate_delegation_item(item, ctx)
        except Exception as exc:  # noqa: BLE001 — reported per item, not raised
            results[idx] = {
                "role": str(role or ""),
                "ok": False,
                "delegation_run_id": None,
                "summary": "",
                "summary_truncated": False,
                "error": str(exc),
            }
            continue
        if len(to_run) >= 4 or budget.delegations + len(to_run) >= 4:
            results[idx] = {
                "role": brief["role"],
                "ok": False,
                "delegation_run_id": None,
                "summary": "",
                "summary_truncated": False,
                "error": "Skipped: delegation budget cap is 4 per run; earlier briefs used the remaining slots",
            }
            continue
        if budget.model_calls + getattr(budget, "reserved_model_calls", 0) >= budget.model_limit - 2:
            results[idx] = {
                "role": brief["role"],
                "ok": False,
                "delegation_run_id": None,
                "summary": "",
                "summary_truncated": False,
                "error": "Skipped: model call budget nearly exhausted; aggregate the delegations already completed and finish",
            }
            continue
        to_run.append((idx, brief))
    raw = await asyncio.gather(
        *(delegate(brief, ctx) for _, brief in to_run), return_exceptions=True
    )
    for (idx, brief), result in zip(to_run, raw, strict=True):
        if isinstance(result, Exception):
            results[idx] = {
                "role": brief["role"],
                "ok": False,
                "delegation_run_id": None,
                "summary": "",
                "summary_truncated": False,
                "error": str(result),
            }
        else:
            results[idx] = {
                "role": brief["role"],
                "ok": bool(result.get("ok")),
                "delegation_run_id": result.get("delegation_run_id"),
                "status": result.get("status"),
                "summary": result.get("summary", ""),
                "summary_truncated": bool(result.get("summary_truncated", False)),
                "artifacts": result.get("artifacts", []),
                "total_tokens": result.get("total_tokens", 0),
                "resources_dropped": result.get("resources_dropped", []),
                "requirement_ids_dropped": result.get("requirement_ids_dropped", []),
                "error": result.get("error"),
            }
    merged: list[dict[str, Any]] = [r for r in results if r is not None]
    ok_count = sum(1 for r in merged if r["ok"])
    return {
        "ok": ok_count == len(merged) and bool(merged),
        "ok_count": ok_count,
        "failed_count": len(merged) - ok_count,
        "results": merged,
    }


async def delegate(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    # Parallel mode: same tool id, additive argument — a single-role call
    # below is unchanged, so existing plans, schemas and tests keep working.
    if isinstance(args.get("delegations"), list):
        return await delegate_parallel(args, ctx)
    from forgeflow.runtime.events import get_event_bus
    from forgeflow.runtime.orchestrator import TaskCreate, register_running_run
    from forgeflow.runtime.react_executor import ReactExecutor
    from forgeflow.workspace.models import WorkspaceRunRecord
    from forgeflow.workspace.store import get_workspace_store

    parent, parent_task = ctx.runtime_context, ctx.task_context
    role = _normalize_role(str(args.get("role") or ""))
    if parent is None or parent_task is None or getattr(parent, "delegation_role", None):
        raise PermissionError("Nested or detached delegation is not allowed")
    if role not in ROLE_TOOLS:
        raise ValueError(f"Unknown specialist role; valid roles are: {', '.join(sorted(ROLE_TOOLS))}")
    instruction = args.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 8000:
        raise ValueError("A bounded specialist instruction is required")
    # Unknown resources are dropped, not raised (see _filter_resources):
    # a model that names a repo file or a handoff document instead of a
    # registered resource id must not kill the delegation — the child
    # still runs on its inherited inputs, and the dropped entries are
    # reported in the result so the parent can see what was not used.
    resources, dropped = _filter_resources(args.get("resources", []), ctx)
    dropped = list(dict.fromkeys([*dropped, *(args.get("resources_dropped") or [])]))
    requirements, req_dropped = _filter_requirement_ids(args.get("requirement_ids", []), ctx)
    req_dropped = list(dict.fromkeys([*req_dropped, *(args.get("requirement_ids_dropped") or [])]))
    budget = current_budget.get()
    if budget is None:
        raise PermissionError("Delegation requires the parent's server-owned budget")
    # 2026-10-30 delegation run 28b7563f: the parent hit the 24 model-call
    # ceiling with no aggregation result. The old code raised
    # BudgetExceeded here when the delegation cap (4) was reached, and
    # the model retried the identical delegate call until the model
    # budget ran out — the same loop pattern the resource/requirement
    # gates had before they were changed to drop-and-report. A cap is
    # not a security boundary, so it is reported as a result the parent
    # can aggregate and finish from, never as a raised error to retry.
    # Likewise, starting a child when fewer than 2 model calls remain
    # would starve the parent's own final-answer call, so that is also
    # a returned result, not a child that can never be aggregated.
    if budget.delegations >= 4:
        return {
            "ok": False,
            "delegation_run_id": None,
            "role": role,
            "status": "skipped",
            "artifacts": [],
            "total_tokens": 0,
            "summary": "",
            "resources_dropped": dropped,
            "requirement_ids_dropped": req_dropped,
            "summary_truncated": False,
            "evidence": [],
            "error": "Delegation budget exhausted (4 per run); do not call agent.delegate again, aggregate the delegations already completed",
        }
    if budget.model_calls + getattr(budget, "reserved_model_calls", 0) >= budget.model_limit - 2:
        return {
            "ok": False,
            "delegation_run_id": None,
            "role": role,
            "status": "skipped",
            "artifacts": [],
            "total_tokens": 0,
            "summary": "",
            "resources_dropped": dropped,
            "requirement_ids_dropped": req_dropped,
            "summary_truncated": False,
            "evidence": [],
            "error": "Model call budget nearly exhausted; do not call agent.delegate again, aggregate the evidence already collected and finish",
        }
    budget.consume("delegation")
    child = copy.copy(parent)
    parent_tools = getattr(parent, "allowed_tools", None)
    child.allowed_tools = (
        ROLE_TOOLS[role] & set(parent_tools) if parent_tools is not None else set(ROLE_TOOLS[role])
    )
    child.delegation_role = role
    child._execution_budget = budget
    child.retrieved_context = []
    child.injected_memory = []
    child.injected_skills = [
        {
            key: entry[key]
            for key in ("id", "name", "description", "version", "content_hash")
            if key in entry
        }
        for entry in getattr(parent, "injected_skills", [])
    ]
    child._skill_read_versions = dict(getattr(parent, "_skill_read_versions", {}))
    task = TaskCreate(
        intent=instruction,
        context={
            **_child_inputs(ctx),
            "resources": resources,
            "max_react_iterations": 4,
        },
    )
    run_id = str(uuid4())
    bus = get_event_bus()
    record = register_running_run(
        run_id=run_id,
        thread_id=run_id,
        tenant_id=ctx.tenant_id,
        intent=instruction,
        workflow_type="generic",
        session_id=run_id,
        actor_user_id=ctx.user_id,
        actor_role=ctx.role,
    )
    meta = {
        "run_id": run_id,
        "delegation_parent_run_id": ctx.run_id,
        "role": role,
        "requirement_ids": requirements,
        "status": "running",
    }
    parent_task.context["llm_runtime"].setdefault("delegations", []).append(meta)
    record.llm = dict(meta)
    await get_workspace_store().save(WorkspaceRunRecord.from_run_record(record))
    await bus.emit(ctx.run_id, "run.delegation", dict(meta))
    # 2026-10-30 delegation run 9dccd73d (b81d9c4): the parent hit the
    # 24 model-call ceiling again, with no aggregation. The cap-return
    # fix stops the parent from *starting* a new child when the budget
    # is nearly gone, but it does not stop children that are already
    # running from eating the parent's own final-answer calls: 4
    # parallel children at up to 4 iterations each can consume the
    # whole shared budget, leaving the parent nothing to aggregate
    # with. So while a child runs, 2 model calls are reserved for the
    # parent (released in the finally below, before delegate returns):
    # a child that hits the reserved ceiling fails as a partial child
    # result, not as a parent that can never finish.
    budget.reserved_model_calls += 2
    _parent_reserved = True
    try:
        from forgeflow.runtime.input_scope import current_input_scope, prepare_input_scope

        scope = await prepare_input_scope(task, child)
        scope_token = current_input_scope.set(scope)
        try:
            steps, errors = await ReactExecutor().run(task, child, bus, run_id)
        finally:
            current_input_scope.reset(scope_token)
        reviewed = [
            r
            for r in task.context.get("tool_invocations", [])
            if r.get("status") == "ok"
            and r.get("executed") is True
            and r.get("tool") in ROLE_TOOLS[role]
            and r.get("tool") != "skills.read"
        ]
        if not reviewed:
            errors = [*errors, "子任务没有实际读取或检查任何证据，未完成审阅"]
        record.steps = steps
        record.errors = errors
        record.tool_invocations = list(task.context.get("tool_invocations", []))
        from forgeflow.runtime.artifacts import artifacts_from_invocations

        record.artifacts = artifacts_from_invocations(
            run_id, record.tool_invocations, fallback_created_at=record.created_at
        )
        usage = [
            {**entry, "agent": f"delegate:{role}:{run_id}"}
            for entry in task.context.get("llm_usage", [])
        ]
        record.total_tokens = sum(
            int(entry.get("input_tokens") or 0) + int(entry.get("output_tokens") or 0)
            for entry in usage
        )
        record.status = "failed" if errors else "completed"
        record.outcome = "failure" if errors else "success"
        record.llm = {**task.context.get("llm_runtime", {}), **meta}
        answer = record.llm.get("final_answer") or {}
        return {
            "ok": not errors,
            "delegation_run_id": run_id,
            "role": role,
            "status": record.status,
            "artifacts": record.artifacts,
            "total_tokens": record.total_tokens,
            "summary": str(answer.get("text") or "")[:2000],
            "resources_dropped": dropped,
            "requirement_ids_dropped": req_dropped,
            # Audit item: the old 2000-char slice had no truncation marker,
            # so a parent reading only ``summary`` saw an incomplete conclusion
            # and could not tell. The full artifacts/evidence are untouched.
            "summary_truncated": len(str(answer.get("text") or "")) > 2000,
            "evidence": [
                {
                    "step_id": r.get("step_id"),
                    "tool": r.get("tool"),
                    "result_ref": r.get("result_ref"),
                    "source_ref": (r["payload"] if isinstance(r.get("payload"), dict) else {}).get(
                        "source_ref"
                    ),
                    "source_hash": (r["payload"] if isinstance(r.get("payload"), dict) else {}).get(
                        "source_hash"
                    ),
                }
                for r in record.tool_invocations
                if r.get("status") == "ok"
                and r.get("executed") is True
                and r.get("tool") != "report.render"
            ],
            "error": "; ".join(errors) if errors else None,
        }
    except Exception as exc:
        record.status = "failed"
        record.outcome = "failure"
        record.errors = [str(exc)]
        raise
    except asyncio.CancelledError:
        record.status = "aborted"
        record.outcome = "aborted"
        raise
    finally:
        if _parent_reserved:
            budget.reserved_model_calls = max(0, budget.reserved_model_calls - 2)
        usage = [
            {**entry, "agent": f"delegate:{role}:{run_id}"}
            for entry in task.context.get("llm_usage", [])
        ]
        parent_task.context.setdefault("llm_usage", []).extend(usage)
        record.total_tokens = sum(
            int(entry.get("input_tokens") or 0) + int(entry.get("output_tokens") or 0)
            for entry in usage
        )
        record.llm["usage"] = usage
        meta["status"] = record.status
        record.llm["status"] = record.status
        record.completed_at = datetime.now(UTC).isoformat()
        await get_workspace_store().save(WorkspaceRunRecord.from_run_record(record))
        await bus.emit(ctx.run_id, "run.delegation", dict(meta))
