"""Failed-trace → eval-case conversion (Phase 9 B5).

A failed run's trace becomes a regression case for future datasets:
what was attempted (tool names + statuses), which errors ended it,
and where it came from. Cases carry NO tool output text and NO
model text — only tool names, statuses, and truncated error
signatures — so a case file is safe to keep in-repo alongside the
frozen datasets. A trace case is not a routing case (it has no
expected route); it is the seed a dataset author turns into one, or
a regression fixture a future harness run replays.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

MAX_ERROR_CHARS = 500
MAX_STEPS = 100


def _error_signature(error: object) -> str:
    text = str(error or "").strip()
    return text[:MAX_ERROR_CHARS]


def trace_to_case(
    run_id: str,
    steps: list[dict[str, Any]] | None,
    errors: list[Any] | None,
    *,
    case_id: str | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Build a trace case from a failed run's steps + errors."""
    clean_steps = []
    for step in (steps or [])[:MAX_STEPS]:
        if not isinstance(step, dict):
            continue
        clean_steps.append(
            {
                "tool": str(step.get("tool") or ""),
                "status": str(step.get("status") or ""),
            }
        )
    failed_tools = sorted(
        {s["tool"] for s in clean_steps if s["status"] not in ("ok", "") and s["tool"]}
    )
    return {
        "id": case_id or f"trace-{run_id}",
        "kind": "failed_trace",
        "source_run_id": run_id,
        "failed_tools": failed_tools,
        "error_signatures": [_error_signature(e) for e in (errors or []) if str(e or "").strip()],
        "steps": clean_steps,
        "notes": notes,
    }


def append_trace_case(case: dict[str, Any], path: str | Path) -> Path:
    """Append one trace case as a JSONL line; returns the file path."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n")
    return target
