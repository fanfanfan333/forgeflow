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
