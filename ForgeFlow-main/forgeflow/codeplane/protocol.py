"""The runner ↔ ForgeFlow JSONL contract (INC25 W2, design §2.4 / §6).

This module is the ForgeFlow-side **only** declaration of the wire contract the
``codeplane/runner/run_code_task.py`` subprocess speaks. The runner is a
standalone program (it imports ``openhands`` and must never import ``forgeflow``),
so it re-declares the same literal constants; this module is the canonical list
the engine and the event adapter agree on, and a drift test pins the two.

Wire shape (one JSON object per line on the runner's stdout):

    {"seq", "ts", "phase", "kind", "status", "tool?", "label", "detail?", "data?"}

``phase`` ∈ :data:`PHASES`; the final line is always a ``result`` line carrying
``exit_code`` plus the raw test stdout. ``status`` uses the **one** step-status
vocabulary shared by the planner, the validator and the front-end
(:data:`STEP_STATUSES`) — no second vocabulary is introduced.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "PHASES",
    "KINDS",
    "STEP_STATUSES",
    "STATUS_ALIASES",
    "DEGRADED_VALUES",
    "RESULT_KIND",
    "JOB_KEYS",
    "encode_line",
    "decode_line",
    "normalize_status",
]

#: Runner phases (design §2.4).
PHASES: tuple[str, ...] = (
    "engine_ready",
    "workspace",
    "plan",
    "action",
    "observation",
    "test",
    "diff",
    "done",
    "error",
)

#: Event kinds the adapter knows how to label.
KINDS: tuple[str, ...] = (
    "engine",
    "state",
    "action",
    "observation",
    "message",
    "hook",
    "test",
    "result",
    "timeout",
    "approval",
    "diff",
    "other",
)

#: The single step-status vocabulary (design §10) — same set the planner, the
#: validator (``validation/validator.py::_UNRUN_STATUSES``) and the front-end
#: (``frontend/src/views/runs/realRun.ts::stepStatusToStageStatus``) use.
STEP_STATUSES: tuple[str, ...] = (
    "ok",
    "error",
    "unavailable",
    "refused",
    "blocked",
    "not_applicable",
    "running",
    "awaiting_approval",
    "pending_approval",
    "paused",
)

#: Reader-side alias applied to a legacy writer status (mirrors
#: ``runtime/planning.py::LEGACY_STATUS_ALIASES``).
STATUS_ALIASES: dict[str, str] = {"skipped": "blocked"}

#: The registered ``codeplane.degraded`` values (design §7). An unregistered value
#: is passed through verbatim by the engine (the front-end then gives a neutral
#: note rather than inventing a cause).
DEGRADED_VALUES: tuple[str, ...] = (
    "engine_unavailable",
    "model_unavailable",
    "timeout",
    "runner_crashed",
    "parse_failed",
    # INC25 P0-C — the runner truncated the agent at its round budget. This is an
    # *error* (the run happened but did not finish), NOT "unavailable": it must
    # never render as ``ok``. Registered here so it is a first-class value.
    "max_rounds",
)

#: The terminal line's ``kind``.
RESULT_KIND = "result"

#: The keys of the job object the engine writes to the runner's stdin.
JOB_KEYS: tuple[str, ...] = (
    "run_id",
    "task_intent",
    "workspace_path",
    "model",
    "base_url",
    "api_key",
    "max_rounds",
    "wall_timeout_s",
    "test_command",
    "language_hint",
    # INC25 fix — the LLM knobs the runner applies to ``LLM(...)`` (see
    # ``runner/run_code_task.py::_llm_kwargs``).
    "reasoning_effort",
    "num_ctx",
    "temperature",
)


def normalize_status(raw: str | None) -> str:
    """Map a (possibly legacy) status onto the current wire vocabulary.

    Delegates to ``runtime.planning.normalize_status`` when importable (the single
    source of truth) so the alias map can never drift; falls back to the local
    alias map otherwise (avoids importing the runtime package at module load).
    """
    value = str(raw or "").strip().lower()
    try:
        from forgeflow.runtime.planning import normalize_status as _canonical

        return _canonical(value)
    except Exception:  # noqa: BLE001 — fallback keeps a partial import working
        return STATUS_ALIASES.get(value, value)


def encode_line(event: dict[str, Any]) -> str:
    """Serialise one event to a single JSONL line (no trailing newline)."""
    return json.dumps(event, ensure_ascii=False)


def decode_line(line: str) -> dict[str, Any] | None:
    """Parse one JSONL line; ``None`` when it is blank or not a JSON object."""
    text = (line or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None
