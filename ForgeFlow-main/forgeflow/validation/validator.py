"""Validation Layer — success/failure judgement (docs §6.6).

Reuses the shape of ``evaluation/metrics`` but stays deterministic and
dependency-free so the failure→replan branch can be exercised offline.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

_SUCCESS_STATUSES = {"completed", "success", "done", "ok"}
_FAILURE_STATUSES = {"failed", "failure", "error"}
_ABORTED_STATUSES = {"aborted", "cancelled", "canceled"}


@dataclass
class Verdict:
    """The validator's conclusion for a run (docs §2 ② input)."""

    success: bool
    outcome: str                 # success | failure | aborted
    score: float = 0.0           # 0.0 .. 1.0
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def validate(run: Any) -> Verdict:
    """Judge whether ``run`` succeeded. Deterministic, no LLM required.

    Rules (in order):
      1. explicit ``status`` in a known terminal set wins;
      2. any recorded ``errors`` ⇒ failure;
      3. otherwise: success when at least one step ran, else failure.
    """
    status = str(_get(run, "status", "") or "").lower()
    errors = list(_get(run, "errors") or [])
    steps = list(_get(run, "steps") or [])
    reasons: list[str] = []

    if status in _ABORTED_STATUSES:
        reasons.append(f"run aborted (status={status})")
        return Verdict(False, "aborted", 0.1, reasons, {"steps": len(steps), "errors": len(errors)})
    if status in _FAILURE_STATUSES:
        reasons.append(f"run failed (status={status})")
        reasons.extend(str(e) for e in errors)
        return Verdict(False, "failure", 0.0, reasons, {"steps": len(steps), "errors": len(errors)})
    if errors:
        reasons.append(f"{len(errors)} error(s) recorded")
        reasons.extend(str(e) for e in errors)
        return Verdict(False, "failure", 0.2, reasons, {"steps": len(steps), "errors": len(errors)})
    if status in _SUCCESS_STATUSES:
        reasons.append(f"run completed (status={status})")
        return Verdict(True, "success", 1.0, reasons, {"steps": len(steps), "errors": 0})
    if steps:
        reasons.append("run produced steps with no errors (implicit success)")
        return Verdict(True, "success", 0.8, reasons, {"steps": len(steps), "errors": 0})

    reasons.append("no steps executed and no terminal status — treating as failure")
    return Verdict(False, "failure", 0.0, reasons, {"steps": 0, "errors": 0})
