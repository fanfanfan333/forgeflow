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


def _validate_delegation_item(item: Any, ctx: Any) -> dict[str, Any]:
    """Validate one specialist brief without starting it.

    Mirrors the pre-flight checks in :func:`delegate` (role, bounded
    instruction, parent resources, parent requirements, no nesting) so a
    parallel batch fails *before* any child starts, instead of leaving a
    half-started fan-out when the second brief is invalid.
    """
    parent, parent_task = ctx.runtime_context, ctx.task_context
    if parent is None or parent_task is None or getattr(parent, "delegation_role", None):
        raise PermissionError("Nested or detached delegation is not allowed")
    if not isinstance(item, dict):
        raise ValueError("Each delegation must be an object")
    role = str(item.get("role") or "")
    if role not in ROLE_TOOLS:
        raise ValueError("Unknown specialist role")
    instruction = item.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 8000:
        raise ValueError("A bounded specialist instruction is required")
    resources = item.get("resources", [])
    allowed_resources = parent_task.context.get("resources") or []
    if not isinstance(resources, list) or any(
        not isinstance(r, str) or r not in allowed_resources for r in resources
    ):
        raise PermissionError("Delegation resources must be selected parent resources")
    requirements = item.get("requirement_ids", [])
    known = {
        r["id"]
        for r in parent_task.context.get("llm_runtime", {})
        .get("acceptance", {})
        .get("requirements", [])
    }
    if not isinstance(requirements, list) or any(
        not isinstance(r, str) or r not in known for r in requirements
    ):
        raise ValueError("Unknown parent requirement")
    return {
        "role": role,
        "instruction": instruction,
        "resources": resources,
        "requirement_ids": requirements,
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
    role = str(args.get("role") or "")
    if parent is None or parent_task is None or getattr(parent, "delegation_role", None):
        raise PermissionError("Nested or detached delegation is not allowed")
    if role not in ROLE_TOOLS:
        raise ValueError("Unknown specialist role")
    instruction = args.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 8000:
        raise ValueError("A bounded specialist instruction is required")
    resources = args.get("resources", [])
    allowed_resources = parent_task.context.get("resources") or []
    if not isinstance(resources, list) or any(
        not isinstance(r, str) or r not in allowed_resources for r in resources
    ):
        raise PermissionError("Delegation resources must be selected parent resources")
    requirements = args.get("requirement_ids", [])
    known = {
        r["id"]
        for r in parent_task.context.get("llm_runtime", {})
        .get("acceptance", {})
        .get("requirements", [])
    }
    if not isinstance(requirements, list) or any(
        not isinstance(r, str) or r not in known for r in requirements
    ):
        raise ValueError("Unknown parent requirement")
    budget = current_budget.get()
    if budget is None:
        raise PermissionError("Delegation requires the parent's server-owned budget")
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
        intent=instruction, context={"resources": resources, "max_react_iterations": 4}
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
