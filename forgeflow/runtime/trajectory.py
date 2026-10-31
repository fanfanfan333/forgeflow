"""Replayable run trajectory (Trae Agent / DeepSeek Harness pattern, ported).

ForgeFlow already kept a cumulative ``tool_invocations`` trail, a
1:1 plan projection, and a verdict — but no single export that a
later diagnosis could open, filter, and replay from. Callers had to
reassemble steps + invocations + plan from the run record by hand,
which is exactly why delegation loops kept needing a user-supplied
``delegate-run.json``.

A trajectory is a versioned, source-tagged projection, not a new
record: every entry carries ``source`` (``tool`` / ``plan`` /
``verdict`` / ``model``) so a consumer can filter by source the way
DeepSeek Harness's Trajectory view does. Payloads are deliberately
NOT copied in full — an entry carries tool, status, summary, and
latency, plus a ``result_ref`` when the invocation has one — so a
trajectory file stays small and never becomes a second copy of
uploaded file content.
"""

from __future__ import annotations

from typing import Any

TRAJECTORY_VERSION = 1


def export_trajectory(
    *,
    run_id: str,
    intent: str = "",
    tool_invocations: list[dict[str, Any]] | None = None,
    steps: list[dict[str, Any]] | None = None,
    plan: dict[str, Any] | None = None,
    verdict: dict[str, Any] | None = None,
    total_tokens: int = 0,
    total_cost_usd: float = 0.0,
    status: str = "",
    outcome: str = "",
) -> dict[str, Any]:
    """Build a versioned trajectory dict from a run's own records."""
    entries: list[dict[str, Any]] = []
    for index, invocation in enumerate(tool_invocations or []):
        if not isinstance(invocation, dict):
            continue
        entries.append(
            {
                "index": index,
                "source": "tool",
                "tool": invocation.get("tool"),
                "status": invocation.get("status"),
                "executed": invocation.get("executed"),
                "summary": invocation.get("summary"),
                "result_ref": invocation.get("result_ref"),
                "latency_ms": invocation.get("latency_ms"),
                "error": invocation.get("error"),
            }
        )
    if plan:
        entries.append({"source": "plan", "plan": plan})
    if verdict:
        entries.append({"source": "verdict", "verdict": verdict})
    return {
        "version": TRAJECTORY_VERSION,
        "run_id": run_id,
        "intent": intent,
        "status": status,
        "outcome": outcome,
        "total_tokens": total_tokens,
        "total_cost_usd": total_cost_usd,
        "steps": steps or [],
        "entries": entries,
    }


def filter_trajectory(trajectory: dict[str, Any], *, source: str) -> dict[str, Any]:
    """Return a copy of ``trajectory`` with only ``source`` entries."""
    filtered = dict(trajectory)
    filtered["entries"] = [e for e in trajectory.get("entries", []) if e.get("source") == source]
    return filtered
