"""INC46 T28 — Agent Loop **cycle detection** (红线 18, termination).

An Agent Loop that replans can spin: it keeps proposing the **same** action from
the **same** state, observes nothing new, and burns the whole budget producing no
progress. This module detects that shape deterministically:

    same (tool, normalised-args hash) consecutively ``>= threshold`` times **and**
    no state progress ⇒ stop and report ``loop_detected``.

Design rules
------------
* **Normalised args.** Two argument dicts that differ only in key order (or that
  contain equivalent JSON) must fingerprint identically — :func:`normalize_args`
  canonicalises with sorted keys, so the fingerprint is stable across processes.
* **Progress resets the run.** :meth:`LoopGuard.observe` takes ``progress=True``
  when the action *did* change state (a new artefact, a moved cursor, a resolved
  pending); that resets the repeat run, so a legitimate retry-after-change is
  never mistaken for a cycle.
* **Threshold is ">= 3" by default.** The default matches the spec exactly; a
  caller may lower/raise it, but never below ``2`` (a single repeat is not a
  cycle).
* **Pure + stateful split.** :func:`fingerprint` / :func:`normalize_args` are
  pure; :class:`LoopGuard` is the thin stateful wrapper the loop uses and keeps
  the audit trail of what it saw.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

__all__ = [
    "DEFAULT_REPEAT_THRESHOLD",
    "normalize_args",
    "fingerprint",
    "LoopSignal",
    "LoopGuard",
    "StuckPattern",
    "detect_stuck_patterns",
]

#: "连续 >= 3 次且无状态进展 ⇒ 终止" (task book TABLE 36). A cycle needs at least
#: two repeats to be a *consecutive run* plus the triggering occurrence, so the
#: floor is 2.
DEFAULT_REPEAT_THRESHOLD = 3


def normalize_args(args: Any) -> str:
    """Canonical JSON for an argument mapping (sorted keys, stable separators).

    A non-mapping / unserialisable value degrades to its ``repr`` rather than
    raising — cycle detection must never break a loop (it may only fail to
    *detect*, which the budget still bounds).
    """
    try:
        return json.dumps(args or {}, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return repr(args)


def fingerprint(tool: str, args: Any) -> str:
    """A stable ``sha256`` over ``(tool, normalize_args(args))``.

    The tool and the normalised args are separated by a NUL so
    ``("ab", {...})`` can never collide with ``("a", {"b": ...})``.
    """
    payload = f"{tool or ''}\x00{normalize_args(args)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LoopSignal:
    """A detected cycle: the repeated action and how many times it repeated."""

    tool: str
    args_hash: str
    repeats: int
    threshold: int
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StuckPattern:
    """A no-progress shape the identical-call guard cannot see.

    OpenHands ``StuckDetector`` pattern, scoped to ForgeFlow's own
    tool records: the existing guards only stop a third *identical*
    tool+args call, so they miss the same error with varying args,
    the same observation with varying args, and A↔B ping-pong.
    Detection is pure (no loop state); the loop warns once per
    pattern per run and forces a final answer only when the same
    pattern is still present a full turn later, so the model always
    gets one turn to respond to the guidance.
    """

    key: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def detect_stuck_patterns(records: list[Any], *, window: int = 12) -> list[StuckPattern]:
    """Scan the recent tool trail for no-progress patterns.

    Only the last ``window`` records are considered, and the
    terminal ``report.render`` step is excluded. Returns at most one
    pattern per shape, in detection order.
    """
    trail = [
        r
        for r in records
        if isinstance(r, dict) and str(r.get("tool") or "") != "report.render"
    ][-window:]
    patterns: list[StuckPattern] = []
    error_counts: dict[tuple[str, str], int] = {}
    for r in trail:
        if r.get("status") in ("error", "unavailable", "refused"):
            key = (str(r.get("tool") or ""), str(r.get("error") or r.get("summary") or ""))
            error_counts[key] = error_counts.get(key, 0) + 1
    for (tool, err), count in error_counts.items():
        if count >= 3:
            patterns.append(
                StuckPattern(
                    key=f"same_error:{tool}:{hashlib.sha256(err.encode('utf-8')).hexdigest()[:12]}",
                    message=f"工具 '{tool}' 已 {count} 次返回相同错误：{err[:200]}",
                )
            )
    observation_counts: dict[tuple[str, str], int] = {}
    for r in trail:
        if r.get("status") == "ok" and r.get("executed") is True and r.get("result_ref"):
            key = (str(r.get("tool") or ""), str(r.get("result_ref") or ""))
            observation_counts[key] = observation_counts.get(key, 0) + 1
    for (tool, ref), count in observation_counts.items():
        if count >= 3:
            patterns.append(
                StuckPattern(
                    key=f"same_observation:{tool}:{ref}",
                    message=f"工具 '{tool}' 已 {count} 次返回相同结果（{ref}），参数变化但观察未变",
                )
            )
    tools = [str(r.get("tool") or "") for r in trail]
    if len(tools) >= 6:
        last6 = tools[-6:]
        if len(set(last6)) == 2 and all(last6[i] == last6[i % 2] for i in range(6)):
            a, b = sorted(set(last6))
            patterns.append(
                StuckPattern(
                    key=f"ping_pong:{a}:{b}",
                    message=f"工具调用在 '{a}' 与 '{b}' 之间来回 ping-pong 已 3 轮",
                )
            )
    return patterns


class LoopGuard:
    """Stateful cycle detector for **one** Agent Loop run.

    The loop calls :meth:`observe` after each action with ``progress`` telling
    whether that action changed any state. When the same
    ``(tool, normalized-args)`` repeats in a run of ``threshold`` consecutive
    no-progress actions, :meth:`observe` returns a :class:`LoopSignal` and the
    loop must terminate with ``status="loop_detected"``.
    """

    def __init__(self, threshold: int = DEFAULT_REPEAT_THRESHOLD) -> None:
        self.threshold = max(2, int(threshold))
        self._last: str | None = None
        self._run = 0
        self.observations = 0
        self.signals: list[LoopSignal] = []

    def observe(self, tool: str, args: Any, *, progress: bool = False) -> LoopSignal | None:
        """Record one action; return a :class:`LoopSignal` when a cycle is found.

        Args:
            tool: the tool / action id.
            args: its arguments (fingerprinted after normalisation).
            progress: whether the action changed state. ``True`` resets the run.
        """
        self.observations += 1
        if progress:
            # State moved on — a legitimate "try again" is not a cycle.
            self._last = None
            self._run = 0
            return None

        fp = fingerprint(tool, args)
        if fp == self._last:
            self._run += 1
        else:
            self._last = fp
            self._run = 1

        if self._run >= self.threshold:
            signal = LoopSignal(
                tool=str(tool or ""),
                args_hash=fp,
                repeats=self._run,
                threshold=self.threshold,
                message=(
                    f"检测到循环：工具 '{tool}' 以相同参数连续执行 {self._run} 次且无状态进展"
                    f"（阈值 {self.threshold}）——终止循环并上报"
                ),
            )
            if all(s.args_hash != fp for s in self.signals):
                self.signals.append(signal)
            return signal
        return None

    @property
    def tripped(self) -> bool:
        return bool(self.signals)

    def to_dict(self) -> dict[str, Any]:
        return {
            "threshold": self.threshold,
            "observations": self.observations,
            "tripped": self.tripped,
            "signals": [s.to_dict() for s in self.signals],
        }
