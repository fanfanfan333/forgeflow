"""Checkpoint-driven resume (8-feature program, Phase 9, A1).

The missing half of Phase 7: checkpoints could be WRITTEN (round end
+ approval boundary) and gated (``verify_resume``), but no caller
turned them into a continued run. This module is that caller.

Semantics (deliberate, and narrower than "resume anything"):

* The source run must be ``interrupted`` / ``awaiting_approval`` /
  ``failed`` (``verify_resume`` enforces it); ``running`` / ``queued`` /
  ``completed`` refuse. A checkpoint never authorizes by itself.
* The resumed run is a NEW run (``parent_run_id`` = source run, same
  session): it inherits the source run's spent model, tool, and
  delegation budget (a resume is not a fresh ceiling), seeded records
  (the source run's raw invocations up to the checkpoint's trajectory
  cursor), the checkpoint's acceptance snapshot as history, and a
  compact evidence summary in a HumanMessage (evidence, never
  instructions). The source run is claimed on the first resume or
  fork; a second call is refused. Round numbers restart for the new
  run; the checkpoint round is audit metadata
  (``resumed_from_checkpoint``).
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


#: Fork is a new lineage, but not a second writer beside a live run.
#: Terminal outcomes and paused/resumable outcomes may be forked.
#: ``running`` / ``queued`` stay exclusive to the run that owns them.
_FORKABLE_STATUSES = frozenset(
    {
        "interrupted",
        "awaiting_approval",
        "failed",
        "paused",
        "completed",
        "aborted",
        "cancelled",
        "expired",
        "rejected",
        "resumed",
    }
)


def _lineage_budget(record: Any) -> dict[str, Any] | None:
    """Source execution budget to carry into the resumed run, if recorded."""
    raw = (getattr(record, "llm", None) or {}).get("execution_budget")
    if not isinstance(raw, dict):
        return None
    needed = (
        "model_calls",
        "tool_calls",
        "delegations",
        "elapsed_seconds",
        "model_limit",
        "tool_limit",
        "seconds_limit",
    )
    if any(key not in raw for key in needed):
        return None
    return dict(raw)


def _find_checkpoint(store: Any, tenant: str, run_id: str, checkpoint_id: str) -> Any | None:
    """Find one checkpoint by id, paging past the newest-first window.

    ``history`` returns at most ``limit`` rows, newest first, so a
    fixed limit makes every older checkpoint unfindable (the old
    ``limit=100`` lookup reported a real, older checkpoint as 404).
    Escalate the window until the id is found or a short page proves
    the history is exhausted.
    """
    limit = 100
    while True:
        candidates = store.history(tenant, run_id, limit=limit)
        for candidate in candidates:
            if str(candidate.id) == checkpoint_id:
                return candidate
        if len(candidates) < limit:
            return None
        limit *= 4


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
    lease_owner: str | None = None,
) -> Any:
    """Resume a run from its latest (or named) checkpoint; returns a RunHandle.

    ``fork=True`` (Phase 9 A3) starts a new lineage from the checkpoint
    when the source is terminal or paused. A ``running`` or ``queued``
    source is refused: forking it would put a second writer beside the
    live run. Schema and approval re-verification still apply; approval
    checkpoints are refused either way. The source run is claimed on
    the first successful gate, so a repeated resume or fork does not
    mint another full budget.

    The resume operation itself runs under the source run's session
    lease (forgeflow.runtime.run_lease): resuming is execution, so it
    must not race the run's owner or a second resumer. The lease is
    acquired before any resume work and released when the resume
    returns — the resumed execution is a NEW run that leases itself
    inside ``run_task``. A held lease refuses through the same channel
    as every other resume gate.

    Raises ``CheckpointNotFound`` (no checkpoint), ``CheckpointResumeRefused``
    (gate failure, approval-kind, trajectory gone, or run leased) — the
    router maps these to 404 / 409.
    """
    from forgeflow.runtime.run_lease import (
        DEFAULT_LEASE_TTL_SECONDS,
        default_lease_owner,
        get_run_lease_store,
    )

    lease_store = get_run_lease_store()
    lease = lease_store.acquire(
        tenant, run_id, lease_owner or default_lease_owner(), DEFAULT_LEASE_TTL_SECONDS
    )
    if lease is None:
        holder = lease_store.peek(tenant, run_id)
        holder_name = holder.owner if holder is not None else "another worker"
        holder_until = holder.expires_at.isoformat() if holder is not None else "unknown expiry"
        raise CheckpointResumeRefused(
            f"run {run_id} is already leased by {holder_name} until {holder_until}; "
            "refusing concurrent resume"
        )
    try:
        return await _resume_run_from_checkpoint_locked(
            tenant=tenant,
            user_id=user_id,
            role=role,
            run_id=run_id,
            checkpoint_id=checkpoint_id,
            fork=fork,
        )
    finally:
        # Token-checked inside the store: a lease that was taken over
        # in the meantime is not ours to release.
        lease_store.release(lease)


async def _resume_run_from_checkpoint_locked(
    *,
    tenant: str,
    user_id: str,
    role: str,
    run_id: str,
    checkpoint_id: str | None = None,
    fork: bool = False,
) -> Any:
    """The resume gates + dispatch, run while holding the source lease."""
    from forgeflow.runtime.dispatcher import get_run_dispatcher, load_run_header
    from forgeflow.runtime.orchestrator import (
        RequestContext,
        TaskCreate,
        strip_reserved_context,
    )

    record = await load_run_header(tenant, run_id)
    if record is None or record.tenant_id != tenant:
        raise CheckpointNotFound(f"run {run_id} not found")
    store = get_checkpoint_store()
    if checkpoint_id:
        checkpoint = _find_checkpoint(store, tenant, run_id, str(checkpoint_id))
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
    if fork and str(record.status) not in _FORKABLE_STATUSES:
        raise CheckpointResumeRefused(
            f"cannot fork run status {record.status!r}; "
            "fork only terminal or paused runs"
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
    if not store.claim_resume(tenant, run_id):
        raise CheckpointResumeRefused(
            "source run already resumed from its checkpoint; "
            "resume the descendant run instead of minting another budget"
        )
    # Declared inputs are caller-supplied context from the source
    # run's record: ``_``-prefixed keys are platform-owned (a stale
    # ``_pending_approval`` grant mapping, ``_delegation_depth``, …)
    # and must never be carried into the new run verbatim — every
    # other task-creation entry point strips them for the same
    # reason.
    declared = strip_reserved_context(
        dict(getattr(record, "declared_inputs", None) or {})
    )
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
    lineage = _lineage_budget(record)
    if lineage is not None:
        task._lineage_budget = lineage
    # The resumed run continues the SOURCE run's work, so it runs
    # under the source run's actor role — not the (possibly more
    # privileged) role of whoever pressed resume. The caller's role
    # is only a fallback when the source record carries none.
    source_role = str(getattr(record, "actor_role", "") or "")
    ctx = RequestContext(tenant_id=tenant, user_id=user_id, role=source_role or role)
    handle = await get_run_dispatcher().dispatch(
        task, ctx, session_id=getattr(record, "session_id", "") or "", parent_run_id=run_id
    )
    return handle
