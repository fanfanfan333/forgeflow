"""Queue worker (A5 slice 1).

Claims due rows, runs each through the same
:func:`forgeflow.runtime.orchestrator.run_task` entry ``POST /tasks``
and the event dispatcher use, then completes or fails the row. The
worker runs as the platform service identity: the run's ``user_id``
is the enqueueing owner (trusted — the enqueue was authenticated),
but the ``role`` is server-resolved to ``service`` (the narrow
"cron / dispatcher" identity, ``execute:workflows`` only), never
taken from the queued payload — mirroring the events dispatcher.

A crashed worker never loses a request: its claimed rows keep their
lease only until ``lease_seconds`` passes, after which the next pass
reaps them back to ``queued``. A worker whose lease expired mid-run
cannot complete or fail the row (the store verifies ``claimed_by`` +
a live lease), so a reaped-and-reclaimed row is executed by exactly
one worker at a time.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from forgeflow.queue.heartbeats import default_hostname, get_heartbeat_store
from forgeflow.queue.schedules import (
    FireReport,
    fire_due_schedules,
    get_schedule_store,
)
from forgeflow.queue.store import (
    DEFAULT_LEASE_SECONDS,
    QueuedTask,
    get_task_queue_store,
)

logger = logging.getLogger(__name__)

__all__ = ["QueueWorker", "SchedulerPassReport", "WorkerPassReport"]

#: The role a queued run executes under — server-resolved, never
#: taken from the queued payload (see module docstring).
WORKER_ROLE = "service"


@dataclass
class WorkerPassReport:
    """Counts for one worker pass (reap + claim + execute)."""

    reaped: int = 0
    claimed: int = 0
    done: int = 0
    failed: int = 0
    dead: int = 0
    skipped: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "reaped": self.reaped,
            "claimed": self.claimed,
            "done": self.done,
            "failed": self.failed,
            "dead": self.dead,
            "skipped": self.skipped,
        }


@dataclass
class SchedulerPassReport:
    """Counts for one scheduler pass (A5 slice 2: due schedules
    fired into the queue). Kept separate from WorkerPassReport so
    the queue pass shape — asserted by the slice-1 tests — never
    changes."""

    fired: int = 0
    schedules: int = 0

    def to_dict(self) -> dict[str, int]:
        return {"fired": self.fired, "schedules": self.schedules}


class QueueWorker:
    """Long-running queue consumer (same start/stop shape as the
    approval-escalation job). The store is sync; every store call is
    offloaded with ``asyncio.to_thread`` so the loop never blocks.
    """

    def __init__(
        self,
        *,
        store: Any = None,
        schedule_store: Any = None,
        heartbeat_store: Any = None,
        poll_seconds: int = 5,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        batch_size: int = 5,
        worker_id: str | None = None,
        hostname: str | None = None,
        run_task_fn: Any = None,
    ) -> None:
        self._store = store
        self._schedule_store = schedule_store
        self._heartbeat_store = heartbeat_store
        self.poll_seconds = max(1, int(poll_seconds))
        self.lease_seconds = max(10, int(lease_seconds))
        self.batch_size = max(1, int(batch_size))
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:12]}"
        self.hostname = hostname or default_hostname()
        self._started_at = datetime.now(UTC)
        # Injectable for tests; the real entry is resolved lazily so
        # importing this module never pulls in the orchestrator.
        self._run_task_fn = run_task_fn
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.passes: list[WorkerPassReport] = []
        self.scheduler_passes: list[SchedulerPassReport] = []

    @property
    def store(self) -> Any:
        if self._store is None:
            self._store = get_task_queue_store()
        return self._store

    @property
    def schedule_store(self) -> Any:
        if self._schedule_store is None:
            self._schedule_store = get_schedule_store()
        return self._schedule_store

    @property
    def heartbeat_store(self) -> Any:
        if self._heartbeat_store is None:
            self._heartbeat_store = get_heartbeat_store()
        return self._heartbeat_store

    async def _beat(self, report: dict[str, Any] | None = None) -> None:
        """Upsert this worker's heartbeat (best-effort).

        A failed beat must never fail a pass: the heartbeat is
        observability, not the work. A dead store logs one warning
        per pass and the worker carries on.
        """
        payload = {"poll_seconds": self.poll_seconds, **(report or {})}
        try:
            await asyncio.to_thread(
                self.heartbeat_store.beat,
                self.worker_id,
                hostname=self.hostname,
                started_at=self._started_at,
                last_report=payload,
            )
        except Exception as exc:  # noqa: BLE001 — heartbeat is best-effort
            logger.warning("worker heartbeat failed (worker=%s): %s", self.worker_id, exc)

    async def run_scheduler_pass(self) -> SchedulerPassReport:
        """One scheduler pass (A5 slice 2): fire due schedules into
        the queue. A firing failure must not kill the queue pass, so
        a bad schedule is logged and skipped, never raised."""
        try:
            fired = await asyncio.to_thread(fire_due_schedules, self.schedule_store, self.store)
        except Exception as exc:  # noqa: BLE001 — scheduler is best-effort per pass
            logger.warning("scheduler pass failed: %s", exc)
            fired = FireReport()
        report = SchedulerPassReport(fired=fired.fired, schedules=fired.schedules)
        self.scheduler_passes.append(report)
        return report

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "QueueWorker started | worker=%s poll=%ds lease=%ds batch=%d",
            self.worker_id,
            self.poll_seconds,
            self.lease_seconds,
            self.batch_size,
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        logger.info("QueueWorker stopped | worker=%s", self.worker_id)

    async def run_once(self) -> WorkerPassReport:
        """One pass: fire due schedules, reap stale leases, claim due
        rows, execute them. The scheduler runs first so a schedule
        fired in this pass can be claimed in the same pass. The pass
        ends with a heartbeat upsert (best-effort, never fatal)."""
        sched = await self.run_scheduler_pass()
        report = WorkerPassReport()
        store = self.store
        report.reaped = await asyncio.to_thread(store.reap_expired_leases)
        claimed = await asyncio.to_thread(
            store.claim,
            self.worker_id,
            limit=self.batch_size,
            lease_seconds=self.lease_seconds,
        )
        report.claimed = len(claimed)
        for row in claimed:
            await self._execute_one(row, report)
        self.passes.append(report)
        await self._beat({**report.to_dict(), **sched.to_dict()})
        return report

    async def _execute_one(self, row: QueuedTask, report: WorkerPassReport) -> None:
        store = self.store
        running = await asyncio.to_thread(store.mark_running, row.tenant_id, row.id, self.worker_id)
        if running is None:
            # Lease lost between claim and execution (reaped and
            # re-claimed by another worker) — do not touch the row.
            report.skipped += 1
            return
        try:
            handle = await self._run_task(row)
        except Exception as exc:  # noqa: BLE001 — a failed run is a retry, not a crash
            logger.warning(
                "queue task %s failed (attempt %d): %s",
                row.id,
                row.attempts + 1,
                exc,
            )
            failed = await asyncio.to_thread(
                store.fail, row.tenant_id, row.id, self.worker_id, str(exc)
            )
            if failed is None:
                report.skipped += 1
            elif failed.status == "dead":
                report.dead += 1
            else:
                report.failed += 1
            return
        done = await asyncio.to_thread(
            store.complete, row.tenant_id, row.id, self.worker_id, handle.run_id
        )
        if done is None:
            report.skipped += 1
        else:
            report.done += 1

    async def _run_task(self, row: QueuedTask) -> Any:
        """Drive one queued task through the orchestrator.

        The run executes under the row's tenant scope (the
        ``tenant_scope`` contextvar): several durable writers inside
        a run read only the ambient tenant — notably the event log,
        whose ``append`` takes no tenant argument — and without the
        scope their rows carry a NULL tenant and RLS denies them.
        The HTTP path gets the scope from request middleware; this
        background entry point must set it explicitly.
        """
        from forgeflow.runtime.orchestrator import RequestContext, TaskCreate, run_task
        from forgeflow.tenant_scope import tenant_scope

        task = row.task or {}
        ctx = RequestContext(
            tenant_id=row.tenant_id,
            user_id=row.owner_id,
            role=WORKER_ROLE,
        )
        declared = TaskCreate(
            intent=str(task.get("intent") or ""),
            title=str(task.get("title") or ""),
            workflow_type=str(task.get("workflow_type") or "generic"),
            context=dict(task.get("context") or {}),
        )
        run_fn = self._run_task_fn or run_task
        with tenant_scope(row.tenant_id):
            return await run_fn(declared, ctx)

    async def _loop(self) -> None:
        # Startup beat so the worker is visible in GET /queue/workers
        # before its first pass finishes.
        await self._beat()
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001 — the loop must survive a bad pass
                logger.exception("QueueWorker pass failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                continue
