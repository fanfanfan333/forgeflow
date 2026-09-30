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

# INC18 — "did this step actually run?" vocabulary.
#
# A run is only honestly ``success`` when every step that **should** have run did
# run. The three sets below are read from the step's own ``status`` (the same
# vocabulary ``runtime/planning`` writes), and any status this build does not
# know is deliberately NOT counted as "unexecuted" — we cannot claim a step did
# not run when the payload carries no evidence either way.
_RAN_STATUSES = {"ok", "success", "done", "error", "failed", "failure"}
# A candidate that provably did not apply to this task — its absence is correct,
# not a shortfall.
_NOT_APPLICABLE_STATUSES = {"not_applicable", "na"}
# Explicitly did not run — these are what make a run PARTIAL.
#
# ⚠️ 边界（INC18 裁决，勿随意扩充）：`unavailable`（无绑定）与 `refused`
# （非 dev 环境调用 dev 工具）**不在此集合**。它们已计入既有的 failed /
# 不可用口径，把它们也算成「未执行」会让 partial 与既有失败统计**双重计数**、
# 互相打架。既有 failed 计数口径本次**一个字都不改**。
_UNRUN_STATUSES = {
    "blocked",
    "pending",
    "running",
    "in_progress",
    "started",
    "queued",
    "awaiting_approval",
    "pending_approval",
    "paused",
}


def _unrun_steps(steps: list[Any]) -> list[str]:
    """The steps that should have run but provably did not (INC18).

    Returns a human-readable id per step (tool name → step id → index) so the
    verdict's ``reasons`` names **exactly** what is missing instead of a bare
    count. ``not_applicable`` is excluded on purpose: a step that does not apply
    to this task is not a shortfall, and calling it one would be a lie in the
    other direction.
    """
    unrun: list[str] = []
    for i, step in enumerate(steps):
        status = str(_get(step, "status", "") or "").strip().lower()
        if status in _RAN_STATUSES or status in _NOT_APPLICABLE_STATUSES:
            continue
        if status not in _UNRUN_STATUSES:
            # Unknown / absent status — no evidence either way, so we do not
            # claim it. Silently skipping is the conservative reading.
            continue
        label = str(_get(step, "tool") or "").strip()
        if not label:
            label = str(_get(step, "step_id") or "").strip()
        unrun.append(label or f"步骤 {i + 1}")
    return unrun


@dataclass
class Verdict:
    """The validator's conclusion for a run (docs §2 ② input)."""

    success: bool
    outcome: str                 # success | partial | failure | aborted
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

    INC18 — ``partial``: rule 3 used to call a run ``success`` as soon as **one**
    step ran, so a run whose report step never executed still advertised
    "已完成". A run that finished without errors but left an applicable step
    unrun is now ``outcome == "partial"``.

    ⚠️ ``success`` (the flag) and ``outcome`` (the verdict) answer DIFFERENT
    questions and must not be conflated:

      * ``success`` = "does this run need a replan / HITL escalation?" — a
        partial run does NOT (retrying would not conjure the missing input), so
        it stays ``True`` and the replan loop is untouched;
      * ``outcome`` = "how honest is the delivery?" — that is what the record
        persists and what the UI badge shows (部分完成).

    Collapsing the two would either escalate every partial run to a human or
    advertise an incomplete deliverable as complete. Both are unacceptable.
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
        unrun = _unrun_steps(steps)
        if unrun:
            reasons.append(
                f"run completed, but {len(unrun)} 个应执行步骤未执行：{'、'.join(unrun)}"
            )
            return Verdict(
                True,
                "partial",
                0.6,
                reasons,
                {"steps": len(steps), "errors": 0, "unrun_steps": unrun},
            )
        reasons.append(f"run completed (status={status})")
        return Verdict(True, "success", 1.0, reasons, {"steps": len(steps), "errors": 0})
    if steps:
        unrun = _unrun_steps(steps)
        if unrun:
            reasons.append(
                f"run produced steps with no errors, but {len(unrun)} 个步骤未执行："
                f"{'、'.join(unrun)}"
            )
            return Verdict(
                True,
                "partial",
                0.5,
                reasons,
                {"steps": len(steps), "errors": 0, "unrun_steps": unrun},
            )
        reasons.append("run produced steps with no errors (implicit success)")
        return Verdict(True, "success", 0.8, reasons, {"steps": len(steps), "errors": 0})

    reasons.append("no steps executed and no terminal status — treating as failure")
    return Verdict(False, "failure", 0.0, reasons, {"steps": 0, "errors": 0})
