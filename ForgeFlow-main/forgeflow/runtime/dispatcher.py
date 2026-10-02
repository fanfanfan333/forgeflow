"""``RunDispatcher`` — async run dispatch / stop / lifecycle (INC32 ADR-01/04).

Why this module exists
----------------------
Before INC32 the platform had **no** notion of a "running run": ``POST /tasks``
ran the task synchronously to a terminal state before responding, so the client
could never address a live run — the SSE stream could only replay history, the
Stop button had no emit point and the Follow-up parent chain was a dead letter.

The dispatcher makes a run a real, addressable, cancellable entity **without
touching** ``POST /tasks`` (its synchronous semantics are byte-for-byte
unchanged):

  * :meth:`dispatch` pre-registers a ``running`` ``RunRecord`` (so
    ``GET /runs/{id}`` answers immediately), persists the session / parent
    relationship into :mod:`forgeflow.workspace.store`, schedules
    ``run_task`` in the background and returns the handle at once;
  * :meth:`abort` cancels that background task (idempotent, terminal-state
    guarded) and the cancellation is surfaced as a ``run.aborted`` terminal
    event so the SSE stream closes cleanly;
  * :meth:`reconcile_on_start` honestly marks any row left ``running`` by a
    previous process as ``interrupted`` (never a fabricated completion).

State is process-local on purpose (it mirrors ``orchestrator.MemoryRunStore``);
the *relationship facts* are persisted by the workspace store.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from forgeflow.config import get_settings
from forgeflow.repositories.base import new_id
from forgeflow.runtime.events import RunEventBus, get_event_bus
from forgeflow.workspace.models import WorkspaceRunRecord
from forgeflow.workspace.store import get_workspace_store

logger = logging.getLogger(__name__)

__all__ = [
    "RunNotFoundError",
    "RunNotAbortableError",
    "RunDispatcher",
    "get_run_dispatcher",
    "reset_run_dispatcher",
    "is_run_cancelled",
    "hydrate_run_store",
    # INC40 — the public read-side hydration surface (B1). ``ensure_tenant_history``
    # is the ONLY symbol ``runs.py`` imports; the module-private idempotency set
    # ``_HYDRATED_TENANTS`` is never reached across module boundaries.
    "ensure_tenant_history",
    "reset_hydrated_tenants",
]

#: Run statuses from which a Stop is refused — the run already ended.
_TERMINAL_ABORT_REFUSED: frozenset[str] = frozenset({"completed", "failed"})
#: The one status that is already the target of a Stop (idempotent 200).
_ABORTED_STATUS = "aborted"

#: Process-local set of tenants whose persisted history has already been
#: backfilled into the in-process run store (INC40). Module-private on purpose:
#: ``ensure_tenant_history`` / ``reset_hydrated_tenants`` own every read/write,
#: so no other module ever imports this private set (B1). Cleared by
#: ``reset_run_dispatcher`` (B2) so each fresh test/process state re-hydrates.
_HYDRATED_TENANTS: set[str] = set()


class RunNotFoundError(LookupError):
    """The run does not exist, or belongs to another tenant (→ HTTP 404)."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"run not found: {run_id}")
        self.run_id = run_id


class RunNotAbortableError(RuntimeError):
    """The run already reached a terminal ``completed`` / ``failed`` state (→ 409)."""

    def __init__(self, run_id: str, status: str) -> None:
        super().__init__(f"run {run_id} is already terminal (status={status})")
        self.run_id = run_id
        self.status = status


class RunDispatcher:
    """In-process manager for background runs + their cancellation."""

    def __init__(
        self,
        *,
        bus: RunEventBus | None = None,
        max_concurrency: int | None = None,
    ) -> None:
        self._bus = bus
        self._max_concurrency = max_concurrency
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancelled: set[str] = set()
        self._sem: asyncio.Semaphore | None = None

    # -- semaphore / bus ---------------------------------------------------- #
    def _limit(self) -> int:
        raw = self._max_concurrency
        if raw is None:
            raw = getattr(get_settings(), "workspace_max_concurrent_runs", 4)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 4
        return value if value >= 1 else 1

    def _semaphore(self) -> asyncio.Semaphore:
        # Created lazily inside a running loop so the dispatcher can be
        # constructed (e.g. in a test) without an event loop.
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._limit())
        return self._sem

    @property
    def bus(self) -> RunEventBus:
        if self._bus is None:
            self._bus = get_event_bus()
        return self._bus

    # -- dispatch ----------------------------------------------------------- #
    async def dispatch(
        self,
        task: Any,
        ctx: Any,
        *,
        session_id: str = "",
        parent_run_id: str = "",
    ) -> Any:
        """Register a ``running`` run, schedule it in the background, return now.

        The ``run_id`` / ``thread_id`` are minted here so the caller can address
        the run (SSE, Stop, artifact download) the instant this returns. The
        session defaults to the run's own id when the caller supplies none
        (ADR-02: the first run of a session defines it).
        """
        from forgeflow.runtime.orchestrator import RunHandle, register_running_run

        run_id = new_id()
        thread_id = new_id()
        sid = session_id or run_id
        task.context["session_id"] = sid
        task.context["parent_run_id"] = parent_run_id

        record = register_running_run(
            run_id=run_id,
            thread_id=thread_id,
            tenant_id=ctx.tenant_id,
            intent=task.intent,
            workflow_type=task.workflow_type,
            session_id=sid,
            parent_run_id=parent_run_id,
            actor_user_id=ctx.user_id,
            actor_role=ctx.role,
        )
        await self._persist_workspace(record)

        atask = asyncio.create_task(self._run_and_finalize(task, ctx, run_id, thread_id))
        self._tasks[run_id] = atask
        atask.add_done_callback(lambda _t, rid=run_id: self._tasks.pop(rid, None))
        return RunHandle(
            run_id=run_id,
            thread_id=thread_id,
            status="running",
            detail={"session_id": sid, "parent_run_id": parent_run_id},
            session_id=sid,
            parent_run_id=parent_run_id,
        )

    async def _run_and_finalize(
        self, task: Any, ctx: Any, run_id: str, thread_id: str
    ) -> None:
        """Run the task to completion; map cancel → ``aborted``, error → ``failed``."""
        from forgeflow.runtime.orchestrator import mark_run_terminal, run_task

        try:
            async with self._semaphore():
                # ``register_running`` is False: ``dispatch`` already wrote the
                # running header synchronously so the run was addressable before
                # this coroutine first ran.
                await run_task(task, ctx, run_id=run_id, thread_id=thread_id)
        except asyncio.CancelledError:
            self._cancelled.discard(run_id)
            await self._safe_emit(
                run_id,
                "run.aborted",
                {"status": _ABORTED_STATUS, "outcome": _ABORTED_STATUS},
            )
            record = mark_run_terminal(run_id, status=_ABORTED_STATUS, outcome=_ABORTED_STATUS)
            if record is not None:
                await self._persist_workspace(record)
            # Re-raise so the asyncio task really ends as cancelled.
            raise
        except Exception as exc:  # noqa: BLE001 — a failed run must not be a 500
            logger.exception("background run %s failed", run_id)
            await self._safe_emit(
                run_id,
                "run.failed",
                {"status": "failed", "message": str(exc)},
            )
            record = mark_run_terminal(run_id, status="failed")
            if record is not None:
                await self._persist_workspace(record)

    async def _safe_emit(self, run_id: str, event_type: str, data: dict[str, Any]) -> None:
        """Emit an event, swallowing a bus failure (must never mask the outcome)."""
        try:
            await self.bus.emit(run_id, event_type, data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("event emit failed for %s (%s): %s", run_id, event_type, exc)

    async def _persist_workspace(self, record: Any) -> None:
        """Persist the run header projection (best-effort housekeeping)."""
        try:
            await get_workspace_store().save(WorkspaceRunRecord.from_run_record(record))
        except Exception as exc:  # noqa: BLE001 — persistence must never break a run
            logger.warning("workspace persistence skipped for %s: %s", record.run_id, exc)

    # -- stop --------------------------------------------------------------- #
    def is_cancelled(self, run_id: str) -> bool:
        """Whether a Stop has been requested for ``run_id`` (cooperative flag)."""
        return run_id in self._cancelled

    async def abort(self, tenant_id: str | None, run_id: str) -> str:
        """Stop a run; return the resulting status (``"aborted"``).

        Raises:
            RunNotFoundError: unknown run, or one owned by another tenant.
            RunNotAbortableError: the run already ``completed`` / ``failed``.
        """
        from forgeflow.runtime.orchestrator import get_run_store, mark_run_terminal

        record = get_run_store().get(run_id)
        if record is None or (record.tenant_id not in (tenant_id, None)):
            raise RunNotFoundError(run_id)

        status = str(record.status)
        if status in _TERMINAL_ABORT_REFUSED:
            raise RunNotAbortableError(run_id, status)
        if status == _ABORTED_STATUS:
            # Idempotent: an already-stopped run returns the same value, 200.
            return _ABORTED_STATUS

        # Cooperative flag + asyncio cancellation. ``cancel()`` schedules the
        # CancelledError at the task's next await point; the set flag stops the
        # executors from producing any *new* step even mid-turn (AC-36).
        self._cancelled.add(run_id)
        atask = self._tasks.get(run_id)
        if atask is not None and not atask.done():
            atask.cancel()

        # Mark the header aborted now so the route's answer and the store agree
        # immediately; the CancelledError handler re-marks idempotently.
        aborted = mark_run_terminal(run_id, status=_ABORTED_STATUS, outcome=_ABORTED_STATUS)
        if aborted is not None:
            await self._persist_workspace(aborted)
        return _ABORTED_STATUS

    # -- lifecycle ---------------------------------------------------------- #
    async def reconcile_on_start(self) -> int:
        """Honest startup收尾: mark stale ``running`` rows ``interrupted``.

        The in-process task references are necessarily gone after a restart, so a
        run left ``running`` did **not** complete — marking it ``interrupted`` is
        honest; fabricating a completion is not. Best-effort (never blocks
        startup).

        INC40 — history hydration no longer happens here. It moved to the **read
        side, per tenant** (:func:`ensure_tenant_history`), so a tenant's history
        can never be pushed out of a global startup window by other tenants'
        newer rows. ``GET /runs`` / ``GET /runs/{id}`` still recover after a
        restart — but on first read of that tenant, not at boot.
        """
        try:
            count = await get_workspace_store().interrupt_stale_running()
        except Exception as exc:  # noqa: BLE001 — housekeeping must never block startup
            logger.warning("workspace startup reconciliation skipped: %s", exc)
            count = 0
        if count:
            logger.info("workspace_runs: marked %d interrupted run(s) on startup", count)
        return count


def _record_from_header(header: WorkspaceRunRecord) -> Any:
    """Rebuild an in-process ``RunRecord`` from a persisted header (INC33).

    The persisted projection carries the header + artifacts but NOT the run body
    (``steps`` / ``tool_invocations`` / ``plan`` / timeline), so those are
    honestly empty and ``detail_retained`` is ``False`` — the UI then says the
    detail did not survive, instead of showing a confident empty step list.
    ``thread_id`` is not persisted, so it degrades to ``""`` (never fabricated).
    """
    from forgeflow.runtime.orchestrator import RunRecord

    return RunRecord(
        run_id=header.run_id,
        thread_id="",
        tenant_id=header.tenant_id,
        agent_id=None,
        intent=header.intent,
        status=header.status,
        outcome=header.outcome,
        steps=[],
        errors=[],
        created_at=header.created_at,
        completed_at=header.completed_at,
        workflow_type=header.workflow_type,
        session_id=header.session_id,
        parent_run_id=header.parent_run_id,
        actor_user_id=header.actor_user_id or "anonymous",
        actor_role=header.actor_role or "viewer",
        declared_inputs=dict(header.declared_inputs or {}),
        artifacts=[dict(a) for a in (header.artifacts or []) if isinstance(a, dict)],
        detail_retained=False,
    )


async def hydrate_run_store(tenant_id: str, limit: int = 200) -> int:
    """Backfill a tenant's persisted ``workspace_runs`` headers into the store.

    INC33/INC40 — ``GET /runs`` / ``GET /runs/{id}`` read the **process-local**
    ``MemoryRunStore`` (``orchestrator._RUN_STORE``), which is empty after a
    restart, so the history column and detail both went blank even though the
    headers WERE persisted (ADR-02「跨重启可查」). This rebuilds them for one
    tenant, request contract unchanged. A record already present in this process
    **wins** (a live run's richer ``RunRecord`` is never clobbered by the thinner
    persisted header).

    Tenant-scoped on purpose: reading ``list_recent_for_tenant(tenant_id)`` means
    a tenant's own history can never be squeezed out by other tenants' newer
    rows (the INC40 fix). Returns the number of headers hydrated. Best-effort —
    never raises.
    """
    from forgeflow.runtime.orchestrator import get_run_store

    try:
        headers = await get_workspace_store().list_recent_for_tenant(tenant_id, limit)
    except Exception as exc:  # noqa: BLE001 — housekeeping must never block the read
        logger.warning("workspace run-store hydrate skipped: %s", exc)
        return 0

    store = get_run_store()
    hydrated = 0
    for header in headers:
        if not header.run_id or store.get(header.run_id) is not None:
            continue
        store.save(_record_from_header(header))
        hydrated += 1
    if hydrated:
        logger.info(
            "workspace_runs: hydrated %d run header(s) into the run store", hydrated
        )
    return hydrated


def reset_hydrated_tenants() -> None:
    """Clear the in-process "已回填" tenant set (INC40 / B2).

    Exposed as a public symbol so both :func:`reset_run_dispatcher` and tests
    reuse it — the private set is never touched across module boundaries (B1).
    """
    _HYDRATED_TENANTS.clear()


async def ensure_tenant_history(tenant_id: str) -> None:
    """Backfill a tenant's persisted history at most once per process (INC40).

    Public read-side seam: the first time this process serves a tenant's history
    (``GET /runs`` / ``GET /runs/{id}``), :func:`hydrate_run_store` runs once for
    that tenant and the idempotency set is stamped. Subsequent reads skip the
    store round-trip. Idempotent and best-effort (never raises).
    """
    if tenant_id in _HYDRATED_TENANTS:
        return
    await hydrate_run_store(tenant_id)
    _HYDRATED_TENANTS.add(tenant_id)


_DISPATCHER: RunDispatcher | None = None


def get_run_dispatcher() -> RunDispatcher:
    """Process-wide dispatcher singleton (shares the SSE event bus)."""
    global _DISPATCHER
    if _DISPATCHER is None:
        _DISPATCHER = RunDispatcher()
    return _DISPATCHER


def reset_run_dispatcher() -> None:
    """Drop the singleton + the hydration idempotency set. Test helper only.

    INC40 / B2 — every fixture that resets process-local run state (run store +
    dispatcher) must clear ``_HYDRATED_TENANTS`` too: otherwise the *second* test
    hitting the same tenant would see "already hydrated" and skip the backfill
    while its in-process store is empty — a false-empty history that goes red.
    """
    global _DISPATCHER
    _DISPATCHER = None
    reset_hydrated_tenants()


def is_run_cancelled(run_id: str) -> bool:
    """Whether the active dispatcher has a Stop pending for ``run_id``.

    The executors call this at each step boundary (ADR-04 cooperative cancel).
    Returns ``False`` when no dispatcher exists (e.g. the synchronous
    ``POST /tasks`` path), so the default behaviour is byte-identical.
    """
    dispatcher = _DISPATCHER
    return bool(dispatcher is not None and dispatcher.is_cancelled(run_id))
