"""Queue service — validation + enqueue orchestration (A5 slice 1).

The store owns persistence and the state machine; this module owns
the shape of what may be enqueued: a task is the same declaration
``POST /tasks`` accepts (``intent`` / ``title`` / ``workflow_type`` /
``context``), normalised here so the API, the worker and the later
cron/webhook slices cannot drift apart.
"""

from __future__ import annotations

import uuid
from typing import Any

from forgeflow.queue.store import DEFAULT_MAX_ATTEMPTS, QueuedTask, TaskQueueStore

__all__ = ["enqueue_task", "normalise_task_payload"]


def normalise_task_payload(task: dict[str, Any]) -> dict[str, Any]:
    """Validate + normalise a task declaration. Raises ValueError."""
    if not isinstance(task, dict):
        raise ValueError("task must be an object")
    intent = task.get("intent")
    if not isinstance(intent, str) or not intent.strip():
        raise ValueError("task.intent is required (non-empty string)")
    title = task.get("title", "")
    if not isinstance(title, str):
        raise ValueError("task.title must be a string")
    workflow_type = task.get("workflow_type", "generic")
    if not isinstance(workflow_type, str) or not workflow_type.strip():
        raise ValueError("task.workflow_type must be a non-empty string")
    context = task.get("context", {})
    if not isinstance(context, dict):
        raise ValueError("task.context must be an object")
    return {
        "intent": intent.strip(),
        "title": title,
        "workflow_type": workflow_type.strip(),
        "context": dict(context),
    }


def enqueue_task(
    store: TaskQueueStore,
    *,
    tenant_id: str,
    owner_id: str,
    task: dict[str, Any],
    idempotency_key: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> QueuedTask:
    """Validate the payload and enqueue it (idempotent on the key).

    A caller-supplied ``idempotency_key`` makes a re-delivered enqueue
    return the existing row; without one a fresh key is generated, so
    the call is still a single enqueue, never a duplicate of an
    earlier call.
    """
    payload = normalise_task_payload(task)
    key = (idempotency_key or "").strip() or uuid.uuid4().hex
    return store.enqueue(
        tenant_id,
        owner_id=owner_id,
        task=payload,
        idempotency_key=key,
        max_attempts=max(1, int(max_attempts)),
    )
