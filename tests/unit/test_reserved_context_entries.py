"""FF-R1 B4 fix (Codex final retest a1) — every task-creation entry
point strips caller-supplied ``_``-prefixed context keys.

``POST /tasks`` and ``/workspace`` already ran
:func:`strip_reserved_context`; the queue, webhook and event entries
did not, so an authenticated caller could pre-seed platform-owned
keys (notably ``_identical_call_*``) into a run's context. One test
per entry point: submit with a ``_`` key and assert it is gone by the
time the task reaches ``run_task``, while ordinary keys survive.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.asyncio

_PROBE_KEY = "_identical_call_signatures"
_PROBE_VALUE = [["research.search", "{}"], ["research.search", "{}"]]


def _assert_stripped(context: dict) -> None:
    assert not any(str(key).startswith("_") for key in context), context
    assert context.get("keep") == "yes"


# --------------------------------------------------------------------------- #
# POST /tasks                                                                  #
# --------------------------------------------------------------------------- #


async def test_tasks_entry_strips_underscore_keys(monkeypatch):
    from forgeflow.api.routers import tasks as tasks_router
    from forgeflow.rbac.models import UserContext

    captured: dict = {}

    async def _capture(task, ctx, **kwargs):
        captured["task"] = task
        raise RuntimeError("captured")

    async def _allow(*args, **kwargs):
        return SimpleNamespace(allowed=True, requires_approval=False, reason="")

    monkeypatch.setattr(tasks_router, "run_task", _capture)
    monkeypatch.setattr(tasks_router, "evaluate_task_entry", _allow)
    monkeypatch.setattr(tasks_router, "_admission_guard", lambda workflow_type: None)

    request = tasks_router.TaskCreateRequestWithAttachments(
        intent="strip probe",
        workflow_type="generic",
        context={_PROBE_KEY: _PROBE_VALUE, "keep": "yes"},
    )
    user = UserContext(user_id="u-tasks", tenant_id="t-entries", role="admin")
    with pytest.raises(HTTPException):
        await tasks_router.create_task(request, user=user, tenant="t-entries")
    _assert_stripped(captured["task"].context)


# --------------------------------------------------------------------------- #
# /workspace                                                                   #
# --------------------------------------------------------------------------- #


async def test_workspace_entry_strips_underscore_keys(monkeypatch):
    from forgeflow.api.routers import workspace as workspace_router
    from forgeflow.rbac.models import UserContext

    captured: dict = {}

    class _FakeDispatcher:
        async def dispatch(self, task, ctx, **kwargs):
            captured["task"] = task
            raise RuntimeError("captured")

    async def _allow(*args, **kwargs):
        return SimpleNamespace(allowed=True, requires_approval=False, reason="")

    monkeypatch.setattr(workspace_router, "get_run_dispatcher", lambda: _FakeDispatcher())
    monkeypatch.setattr(workspace_router, "_admission_guard", lambda workflow_type: None)
    monkeypatch.setattr(workspace_router, "evaluate_task_entry", _allow)

    request = workspace_router.WorkspaceTaskCreateRequest(
        intent="strip probe",
        workflow_type="generic",
        context={_PROBE_KEY: _PROBE_VALUE, "keep": "yes"},
    )
    user = UserContext(user_id="u-ws", tenant_id="t-entries", role="admin")
    with pytest.raises(RuntimeError, match="captured"):
        await workspace_router.create_workspace_task(request, user=user, tenant="t-entries")
    _assert_stripped(captured["task"].context)


# --------------------------------------------------------------------------- #
# queue (worker is the run_task chokepoint for every queued task)              #
# --------------------------------------------------------------------------- #


async def test_queue_worker_strips_underscore_keys():
    from forgeflow.queue.service import enqueue_task
    from forgeflow.queue.store import InMemoryTaskQueueStore, reset_task_queue_store
    from forgeflow.queue.worker import QueueWorker

    reset_task_queue_store()
    store = InMemoryTaskQueueStore()
    captured: dict = {}

    class _Handle:
        run_id = "run-q"

    async def _capture(task, ctx):
        captured["task"] = task
        return _Handle()

    enqueue_task(
        store,
        tenant_id="t-entries",
        owner_id="u1",
        task={
            "intent": "strip probe",
            "title": "t",
            "workflow_type": "generic",
            "context": {_PROBE_KEY: _PROBE_VALUE, "keep": "yes"},
        },
        idempotency_key="k-strip",
    )
    worker = QueueWorker(store=store, run_task_fn=_capture)
    report = await worker.run_once()
    assert report.done == 1
    _assert_stripped(captured["task"].context)
    reset_task_queue_store()


# --------------------------------------------------------------------------- #
# webhook trigger (template + body contexts are merged, then enqueued)        #
# --------------------------------------------------------------------------- #


def test_webhook_trigger_strips_underscore_keys():
    from datetime import UTC, datetime, timedelta

    from forgeflow.events.processed_events import (
        InMemoryProcessedEventStore,
        reset_processed_event_store,
    )
    from forgeflow.queue.store import InMemoryTaskQueueStore, reset_task_queue_store
    from forgeflow.queue.webhooks import (
        InMemoryWebhookStore,
        create_webhook,
        reset_webhook_store,
        sign_trigger,
        trigger_webhook,
    )

    reset_webhook_store()
    reset_task_queue_store()
    reset_processed_event_store()
    webhook_store = InMemoryWebhookStore()
    queue_store = InMemoryTaskQueueStore()
    processed_store = InMemoryProcessedEventStore()
    template = {
        "intent": "strip probe",
        "title": "weekly",
        "workflow_type": "generic",
        "context": {"keep": "yes", "_template_seed": 1},
    }
    row, secret = create_webhook(
        webhook_store,
        tenant_id="t-entries",
        owner_id="owner-9",
        name="strip-probe",
        task_template=template,
    )
    expires = (datetime.now(UTC) + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    body_task = {"context": {_PROBE_KEY: _PROBE_VALUE}}
    signature = sign_trigger(
        secret,
        tenant_id=row.tenant_id,
        owner_id=row.owner_id,
        task=body_task,
        event_id="evt-strip",
        expires_at=expires,
    )
    queued = trigger_webhook(
        webhook_store,
        queue_store,
        processed_store,
        webhook_id=row.id,
        task=body_task,
        event_id="evt-strip",
        expires_at=expires,
        signature=signature,
    )
    assert queued.status == "queued"
    _assert_stripped(queued.task["context"])
    reset_webhook_store()
    reset_task_queue_store()
    reset_processed_event_store()


# --------------------------------------------------------------------------- #
# event dispatch                                                               #
# --------------------------------------------------------------------------- #


async def test_event_dispatch_strips_underscore_keys(monkeypatch):
    import forgeflow.runtime.orchestrator as orch
    from forgeflow.events.dispatcher import EventDispatcher, WorkflowTrigger

    captured: dict = {}

    async def _capture(task, ctx, **kwargs):
        captured["task"] = task
        return SimpleNamespace(run_id="run-ev", thread_id="thread-ev")

    monkeypatch.setattr(orch, "run_task", _capture)
    dispatcher = EventDispatcher(graphs={})
    trigger = WorkflowTrigger(
        workflow_type="agentflow_task",
        payload={
            "intent": "strip probe",
            "context": {_PROBE_KEY: _PROBE_VALUE, "keep": "yes"},
        },
        user_id="eve",
        role="manager",
        extra={"tenant_id": "t-entries"},
    )
    result = await dispatcher.dispatch(trigger)
    assert result.ok is True
    _assert_stripped(captured["task"].context)
