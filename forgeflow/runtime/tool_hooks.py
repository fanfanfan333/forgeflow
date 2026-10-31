"""Tool hooks (Claude Code PreToolUse / PostToolUse pattern, ported).

ForgeFlow previously had hard-coded gates only — role allowlists,
resource dropping, the code gate — and no registration point where a
caller could inspect, rewrite, or veto a tool call. This module is
that point, and :class:`ToolExecutor` is its single choke point, so
every runtime tool call flows through it.

A hook is a callable ``(event, tool, args, result) -> decision``.
``event`` is ``"pre"``, ``"post"``, or ``"post_failure"``.
A decision is a dict, all keys optional:

- ``action``: ``"allow"`` (default), ``"deny"``, or ``"rewrite"``.
- ``args``: replacement args, only meaningful with ``"rewrite"`` on
  a pre hook.
- ``reason``: human-readable reason, surfaced on a deny.

Ordering guarantee, mirroring Claude's deny-wins rule: a deny from
any pre hook vetoes the call even if another hook allowed it, and a
deny is never overridden by a later hook. Hooks never raise into a
run: a hook that raises is logged and treated as allow, so a buggy
hook cannot break a run — it can only fail to veto it.

The registry is process-global and test-scoped: callers that register
hooks for one run must call :func:`clear_hooks` afterwards, and tests
do so via fixture-style teardown. A per-run registry would be cleaner
but would require threading a registry through every executor, gate,
and handler call site; the global registry with explicit clearing is
the minimal change that still gives a real, testable hook point.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

Hook = Callable[[str, str, dict[str, Any], Any], Any]

_HOOKS: dict[str, list[Hook]] = {"pre": [], "post": [], "post_failure": []}


def register_hook(event: str, hook: Hook) -> None:
    """Register ``hook`` for ``event`` (``pre`` / ``post`` / ``post_failure``)."""
    if event not in _HOOKS:
        raise ValueError(f"Unknown hook event {event!r}; expected pre/post/post_failure")
    _HOOKS[event].append(hook)


def clear_hooks() -> None:
    """Remove all registered hooks (test teardown / run teardown)."""
    for hooks in _HOOKS.values():
        hooks.clear()


async def run_pre_hooks(tool: str, args: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Run pre hooks in registration order.

    Returns ``(effective_args, deny_reason)``. The first deny wins;
    a rewrite replaces args for subsequent hooks and for the call.
    """
    effective = dict(args)
    for hook in list(_HOOKS["pre"]):
        try:
            decision = hook("pre", tool, effective, None)
            if hasattr(decision, "__await__"):
                decision = await decision
        except Exception as exc:  # noqa: BLE001 — a buggy hook must not break a run
            logger.warning("Pre-tool hook for %s raised, treated as allow: %s", tool, exc)
            continue
        if not isinstance(decision, dict):
            continue
        action = decision.get("action", "allow")
        if action == "deny":
            return effective, str(decision.get("reason") or f"Hook denied {tool}")
        if action == "rewrite" and isinstance(decision.get("args"), dict):
            effective = dict(decision["args"])
    return effective, None


async def run_post_hooks(event: str, tool: str, args: dict[str, Any], result: Any) -> None:
    """Run post / post_failure hooks, observe-only, never raising."""
    for hook in list(_HOOKS[event]):
        try:
            decision = hook(event, tool, args, result)
            if hasattr(decision, "__await__"):
                await decision
        except Exception as exc:  # noqa: BLE001 — a buggy hook must not break a run
            logger.warning("Post-tool hook for %s raised, ignored: %s", tool, exc)
