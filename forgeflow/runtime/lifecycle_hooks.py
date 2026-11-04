"""Agent lifecycle hooks (Phase 9 B11).

Run-level counterpart to the tool-level hooks in ``tool_hooks``:
``run_started`` fires once per ``run_task`` call, ``run_finished``
fires once per call (success or failure — a lifecycle hook that
misses error paths is half a lifecycle), and ``attempt_finished``
fires after each executor attempt inside ``_run_task`` (attempt 0 =
the first executor run; replan retries increment it). The graph
path has no attempts and fires no attempt events.

Hooks are observe-only, exactly like post-tool hooks: a raising
hook is logged and ignored, never allowed to break a run. Hooks
receive ``(event, payload)``; payloads are plain dicts (run id,
tenant, intent, attempt, status/error) — never task objects.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

Hook = Callable[[str, dict[str, Any]], Any]

_HOOKS: dict[str, list[Hook]] = {
    "run_started": [],
    "run_finished": [],
    "attempt_finished": [],
}


def register_lifecycle_hook(event: str, hook: Hook) -> None:
    """Register ``hook`` for ``event`` (run_started / run_finished / attempt_finished)."""
    if event not in _HOOKS:
        raise ValueError(
            f"Unknown lifecycle event {event!r}; expected run_started/run_finished/attempt_finished"
        )
    _HOOKS[event].append(hook)


def clear_lifecycle_hooks() -> None:
    """Remove all registered lifecycle hooks (test teardown)."""
    for hooks in _HOOKS.values():
        hooks.clear()


async def run_lifecycle_hooks(event: str, payload: dict[str, Any]) -> None:
    """Run an event's hooks in registration order, observe-only, never raising."""
    for hook in list(_HOOKS[event]):
        try:
            decision = hook(event, dict(payload))
            if hasattr(decision, "__await__"):
                await decision
        except Exception as exc:  # noqa: BLE001 — a buggy hook must not break a run
            logger.warning("Lifecycle hook for %s raised, ignored: %s", event, exc)
