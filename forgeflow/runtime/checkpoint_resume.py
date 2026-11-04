"""Checkpoint-driven resume (8-feature program, Phase 9, A1).

The missing half of Phase 7: checkpoints could be WRITTEN (round end
+ approval boundary) and gated (``verify_resume``), but no caller
turned them into a continued run. This module is that caller.

Semantics (deliberate, and narrower than "resume anything"):

* The source run must be ``interrupted`` / ``awaiting_approval`` /
  ``failed`` (``verify_resume`` enforces it); ``running`` / ``queued`` /
  ``completed`` refuse. A checkpoint never authorizes by itself.
* The resumed run is a NEW run (``parent_run_id`` = source run, same
  session): it gets a FRESH model-call budget, seeded records (the
  source run's raw invocations up to the checkpoint's trajectory
  cursor), the checkpoint's acceptance snapshot as history, and a
  compact evidence summary in a HumanMessage (evidence, never
  instructions). Round numbers restart for the new run; the
  checkpoint round is audit metadata (``resumed_from_checkpoint``).
* Approval-kind checkpoints do NOT resume here: an approved call
  resumes through the existing ``POST /pending-actions/{id}/resume``
  (the grant path, with its own verification). This endpoint
  refuses them with a pointer instead of duplicating that path.
* Trajectory availability is honest: the source run's raw
  invocations live in the process-lifetime run store. When they are
  gone (process restart — hub runs are not persisted), resume
  refuses with "trajectory no longer available" instead of
  fabricating a continuation. Cross-restart resume needs persisted
  trajectories first (a follow-up, not silently papered over).
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

from forgeflow.runtime.checkpoints import (
    CheckpointResumeRefused,
    build_resume_seed,
    get_checkpoint_store,
    verify_resume,
)


class CheckpointNotFound(Exception):
    """No checkpoint matched (unknown id, or none for the run)."""


def _approval_resolver(tenant: str) -> Any:
    from forgeflow.hitl.pending import get_pending_store

    store = get_pending_store()

    def resolve(pending_id: str) -> dict[str, Any] | None:
        action = store.get(tenant, pending_id)
        if action is None:
            return None
        return {
            "status": action.status,
            "resolution": action.resolution,
            "resolved_by": action.resolved_by,
            "expires_at": action.expires_at,
            "payload": dict(action.payload or {}),
        }

    return resolve


async def resume_run_from_checkpoint(
    *,
    tenant: str,
    user_id: str,
    role: str,
    run_id: str,
    checkpoint_id: str | None = None,
    fork: bool = False,
) -> Any:
    """Resume a run from its latest (or named) checkpoint; returns a RunHandle.

    ``fork=True`` (Phase 9 A3) starts a new lineage from the checkpoint
    without requiring the source run to be resumable — the source keeps
    its own status and keeps running if it was running. Schema and
    approval re-verification still apply; approval checkpoints are
    refused either way.

    Raises ``CheckpointNotFound`` (no checkpoint), ``CheckpointResumeRefused``
    (gate failure, approval-kind, or trajectory gone) — the router maps
    these to 404 / 409.
    """
    from forgeflow.runtime.dispatcher import get_run_dispatcher, load_run_header
    from forgeflow.runtime.orchestrator import RequestContext, TaskCreate

    record = await load_run_header(tenant, run_id)
    if record is None or record.tenant_id != tenant:
        raise CheckpointNotFound(f"run {run_id} not found")
    store = get_checkpoint_store()
    if checkpoint_id:
        candidates = store.history(tenant, run_id, limit=100)
        checkpoint = next((c for c in candidates if str(c.id) == checkpoint_id), None)
        if checkpoint is None:
            raise CheckpointNotFound(f"checkpoint {checkpoint_id} not found for run {run_id}")
    else:
        checkpoint = store.latest(tenant, run_id)
        if checkpoint is None:
            raise CheckpointNotFound(f"no checkpoint for run {run_id}")
    if checkpoint.kind == "approval":
        raise CheckpointResumeRefused(
            "approval checkpoints resume via POST /pending-actions/{id}/resume "
            "(the grant path); this endpoint resumes round checkpoints only"
        )
    now_iso = datetime.now(UTC).isoformat()
    verify_resume(
        checkpoint,
        run_status=str(record.status),
        approval_resolver=_approval_resolver(tenant),
        now_iso=now_iso,
        skip_status=fork,
    )
    source_invocations = list(getattr(record, "tool_invocations", None) or [])
    seed = build_resume_seed(checkpoint, source_invocations)
    seed["forked"] = fork
    cursor = int((checkpoint.state or {}).get("trajectory_cursor") or 0)
    if cursor > 0 and not seed["records"]:
        raise CheckpointResumeRefused(
            "source trajectory no longer available (run record gone after restart); "
            "rerun the task from scratch instead of resuming"
        )
    declared = dict(getattr(record, "declared_inputs", None) or {})
    task = TaskCreate(
        intent=str(checkpoint.state.get("intent") or record.intent),
        workflow_type=str(getattr(record, "workflow_type", None) or "generic"),
        context=copy.deepcopy(declared),
    )
    # Seed via the task ATTRIBUTE only: run_task pops tool_invocations /
    # llm_runtime from submitted context (server-owned evidence), so
    # context seeding would be stripped; the loop materialises the
    # attribute into its trail after that pop.
    task._checkpoint_resume = seed
    ctx = RequestContext(tenant_id=tenant, user_id=user_id, role=role)
    handle = await get_run_dispatcher().dispatch(
        task, ctx, session_id=getattr(record, "session_id", "") or "", parent_run_id=run_id
    )
    return handle
