"""Code-plane approval routes (INC25 W2, design §3.3 #38 / §11 U5).

The code-execution plane's human-in-the-loop closure. A code task runs, produces
a diff, and ends ``awaiting_approval``; these two endpoints are how a person
resolves it:

  * ``POST /codeplane/runs/{run_id}/approve`` — approve the change: the pending
    ``ApprovalRecord`` is resolved (so ``/approvals`` stays consistent) and a
    **resume run** is launched that commits the workspace's change onto its own
    branch and raises the ``code_diff`` / ``code_test_report`` artifacts.
  * ``POST /codeplane/runs/{run_id}/reject`` — reject the change: the pending
    ``ApprovalRecord`` is resolved and the workspace is **destroyed** (no change
    is left behind).

Both write the existing nine-field audit channel via
:mod:`forgeflow.codeplane.approval`. The third UI action (重新分析) intentionally
reuses the existing ``POST /runs/{id}/replan`` route (which audits it for a
code-plane run). Versioned file checkpoint inspection and guarded restore are
additional workspace operations; they do not roll back remote side effects.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from forgeflow.api.dependencies import get_current_user
from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.hub_schemas import RunHandleResponse
from forgeflow.codeplane.approval import decide_code_approval, find_code_approval
from forgeflow.codeplane.workspace import get_workspace_manager
from forgeflow.rbac.models import UserContext
from forgeflow.repositories import get_policy_repository
from forgeflow.runtime.orchestrator import RequestContext, TaskCreate, get_run_store, run_task

logger = logging.getLogger(__name__)
router = APIRouter()


class CodeDecisionRequest(BaseModel):
    """Body for ``approve`` / ``reject`` (``note`` is optional and audited)."""

    note: str = ""


async def _load_code_run(run_id: str, tenant: str, user_id: str):
    """Load a tenant-scoped run that is a code-plane run (404 / 409 otherwise)."""
    from forgeflow.runtime.dispatcher import load_run_header

    record = await load_run_header(tenant, run_id)
    if record is None or record.tenant_id != tenant or record.actor_user_id != user_id:
        raise HTTPException(status_code=404, detail="Run not found")
    codeplane = dict(getattr(record, "codeplane", None) or {})
    if not codeplane:
        raise HTTPException(status_code=409, detail="Run is not a code task")
    return record, codeplane


async def _reflect_decision(record, decision, user_id: str) -> None:
    """Write the resolved decision back onto the run's ``codeplane.approval``.

    Keeps ``GET /runs/{id}`` (and thus the UI) consistent with the decision the
    person just made — the same discipline U5 applies to ``GET /approvals``. A
    store that rejects the write is logged, never raised: the decision itself has
    already been persisted on the ``ApprovalRecord``.
    """
    if decision is None:
        return
    codeplane = dict(getattr(record, "codeplane", None) or {})
    if not codeplane:
        return
    codeplane["approval"] = {
        "status": decision.status,
        "approval_id": decision.id,
        "decided_by": user_id,
        "decided_at": decision.resolved_at,
    }
    record.codeplane = codeplane
    try:
        get_run_store().save(record)
        from forgeflow.workspace.models import WorkspaceRunRecord
        from forgeflow.workspace.store import get_workspace_store

        await get_workspace_store().save(WorkspaceRunRecord.from_run_record(record))
    except Exception as exc:  # noqa: BLE001 — the decision is already persisted
        logger.warning("code-plane run reflect failed for %s: %s", record.run_id, exc)


@router.post("/runs/{run_id}/approve", response_model=RunHandleResponse)
async def approve_code_run(
    run_id: str,
    request: CodeDecisionRequest | None = None,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """Approve the code change and run the commit (a **resume run**).

    The decision is written to the ``PolicyRepository`` (so ``GET /approvals`` is
    consistent) and the resume run re-declares the original inputs plus the
    granted approval + the workspace to reuse, so ``code.commit`` performs the
    commit and the artifacts are produced. ``note`` is optional.
    """
    record, codeplane = await _load_code_run(run_id, tenant, user.user_id)
    note = (request.note if request else "") or ""
    repo = get_policy_repository()
    if await find_code_approval(repo, tenant, run_id) is None:
        raise HTTPException(status_code=409, detail="No pending code approval for this run")
    decision = await decide_code_approval(
        repo,
        tenant_id=tenant,
        run_id=run_id,
        decision="approve",
        approver=user.user_id,
        note=note,
        role=user.role,
    )
    if decision is None or decision.status != "approved":
        raise HTTPException(status_code=409, detail="Approval could not be resolved")
    await _reflect_decision(record, decision, user.user_id)

    workspace = dict(codeplane.get("workspace") or {})
    workspace_id = str(workspace.get("workspace_id") or "").strip()
    if not workspace_id:
        # INC25 P0-B — the record's ``codeplane.workspace`` may be absent (an old
        # / degraded record). The workspace manager still maps this run to its
        # live workspace (``_by_run``), so recover the id from there; without it
        # the resume run hands ``code.commit`` no workspace and the commit fails
        # with "工作区路径不可用" even though the isolated change still exists.
        try:
            described = get_workspace_manager().describe(run_id) or {}
        except Exception as exc:  # noqa: BLE001 — a lookup failure degrades to no-resume
            logger.warning("workspace lookup failed for %s: %s", run_id, exc)
            described = {}
        workspace_id = str(described.get("workspace_id") or "").strip()
        if workspace_id and not workspace:
            workspace = dict(described)

    context = dict(getattr(record, "declared_inputs", None) or {})
    context["codeplane_approval"] = "approved"
    if workspace_id:
        context["codeplane_workspace_id"] = workspace_id
    # Carry forward the prior evidence, enriched with the recovered workspace so
    # ``code.commit`` can read the workspace / diff / tests it must reuse.
    prior = dict(codeplane)
    if workspace and not prior.get("workspace"):
        prior["workspace"] = workspace
    context["codeplane_prior"] = prior
    ctx = RequestContext(tenant_id=tenant, user_id=user.user_id, role=user.role)
    task = TaskCreate(
        intent=record.intent,
        title=record.intent[:60],
        workflow_type=str(getattr(record, "workflow_type", "generic") or "generic"),
        context=context,
    )
    handle = await run_task(task, ctx)
    logger.info("code approval | run=%s resume=%s by=%s", run_id, handle.run_id, user.user_id)
    return RunHandleResponse(**handle.to_dict())


@router.post("/runs/{run_id}/reject", response_model=RunHandleResponse)
async def reject_code_run(
    run_id: str,
    request: CodeDecisionRequest | None = None,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """Reject the code change: resolve the approval and **destroy** the workspace.

    Nothing is committed and the isolated workspace is removed, so no change is
    left behind (design §5.2). The decision is audited and reflected on
    ``GET /approvals``.
    """
    record, codeplane = await _load_code_run(run_id, tenant, user.user_id)
    note = (request.note if request else "") or ""
    repo = get_policy_repository()
    if await find_code_approval(repo, tenant, run_id) is None:
        raise HTTPException(status_code=409, detail="No pending code approval for this run")
    decision = await decide_code_approval(
        repo,
        tenant_id=tenant,
        run_id=run_id,
        decision="reject",
        approver=user.user_id,
        note=note,
        role=user.role,
    )
    if decision is None or decision.status != "rejected":
        raise HTTPException(status_code=409, detail="Approval could not be resolved")
    await _reflect_decision(record, decision, user.user_id)

    workspace = dict(codeplane.get("workspace") or {})
    workspace_id = str(workspace.get("workspace_id") or "")
    if workspace_id:
        try:
            get_workspace_manager().release(workspace_id, destroy=True)
        except Exception as exc:  # noqa: BLE001 — cleanup must never break the decision
            logger.warning("workspace destroy failed for %s: %s", workspace_id, exc)
    logger.info("code rejection | run=%s workspace=%s by=%s", run_id, workspace_id, user.user_id)
    return RunHandleResponse(
        run_id=run_id,
        thread_id=record.thread_id,
        status="rejected",
        detail={
            "outcome": "rejected",
            "approval_id": decision.id,
            "workspace_id": workspace_id,
            "workspace_destroyed": bool(workspace_id),
            "decided_by": user.user_id,
            "note": note,
        },
    )


class FileRestoreRequest(BaseModel):
    checkpoint_version: int = Field(..., ge=1)
    expected_checkpoint_version: int = Field(..., ge=1)
    file: str = Field(..., min_length=1, max_length=1000)
    expected_current_hash: str | None = Field(..., pattern=r"^[a-f0-9]{64}$")


@router.get("/workspaces/{workspace_id}/checkpoints")
async def list_file_checkpoints(
    workspace_id: str, tenant: str = Depends(resolve_tenant),
    user: UserContext = Depends(get_current_user),
):
    workspace = get_workspace_manager().get(workspace_id)
    if workspace is None or workspace.tenant_id != tenant:
        raise HTTPException(status_code=404, detail="Workspace not found")
    await _load_code_run(workspace.run_id, tenant, user.user_id)
    try:
        with get_workspace_manager()._checkpoint_lock(workspace):
            return {"workspace_id": workspace_id, "state": workspace.state,
                    "version": len(workspace.checkpoints), "items": list(workspace.checkpoints)}
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/workspaces/{workspace_id}/restore-file")
async def restore_checkpoint_file(
    workspace_id: str, request: FileRestoreRequest,
    user: UserContext = Depends(get_current_user), tenant: str = Depends(resolve_tenant),
):
    from forgeflow.middleware.audit import write_audit_entry
    from forgeflow.rbac.enforcer import RBACEnforcer

    if not RBACEnforcer().check(user.role, "approve", "code"):
        raise HTTPException(status_code=403, detail="File restoration requires approve:code")
    workspace = get_workspace_manager().get(workspace_id)
    if workspace is None or workspace.tenant_id != tenant:
        raise HTTPException(status_code=404, detail="Workspace not found")
    await _load_code_run(workspace.run_id, tenant, user.user_id)
    try:
        result = get_workspace_manager().restore_file(
            workspace_id, tenant_id=tenant, checkpoint_version=request.checkpoint_version,
            expected_version=request.expected_checkpoint_version, relative=request.file,
            expected_hash=request.expected_current_hash,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await write_audit_entry({"user_id": user.user_id, "role": user.role,
                             "action": "code.file.restore", "resource": "code",
                             "resource_id": workspace_id, "outcome": "allowed",
                             "workspace_id": tenant, "metadata": result})
    return result


@router.get("/workspaces/{workspace_id}/checkpoints/{version}")
async def checkpoint_file_versions(
    workspace_id: str, version: int, offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100), tenant: str = Depends(resolve_tenant),
    user: UserContext = Depends(get_current_user),
):
    from pathlib import Path

    from forgeflow.codeplane.checkpoints import file_hash, load, safe_target
    from forgeflow.documents.store import DocArtifactStore

    manager = get_workspace_manager()
    workspace = manager.get(workspace_id)
    if workspace is None or workspace.tenant_id != tenant:
        raise HTTPException(status_code=404, detail="Workspace not found")
    await _load_code_run(workspace.run_id, tenant, user.user_id)
    try:
        with manager._checkpoint_lock(workspace):
            checkpoint = next((c for c in workspace.checkpoints if c["version"] == version), None)
            if checkpoint is None:
                raise HTTPException(status_code=404, detail="Checkpoint not found")
            document = load(workspace, checkpoint, DocArtifactStore(manager.root / "_snapshots"))
            names = sorted(document["files"])
            files = []
            for name in names[offset:offset + limit]:
                entry = document["files"][name]
                files.append({"file": name, "stored_hash": entry["hash"],
                              "current_hash": file_hash(safe_target(Path(workspace.path), name)),
                              "bytes": entry["bytes"]})
            return {"version": len(workspace.checkpoints), "state": workspace.state,
                    "checkpoint_version": version, "total": len(names), "files": files}
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
