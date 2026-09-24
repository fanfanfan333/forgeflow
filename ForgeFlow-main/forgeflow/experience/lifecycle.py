"""Memory lifecycle — score / decay / archive (INC9 §B2, ``目标.md`` §6 P1-八).

Before this increment the hub-level memory was **append-only**: ``save_memory``
added entries and nothing ever scored, decayed, or retired them. Over time the
recall paths would fill with stale, never-reused facts.

This module is the **pure** half of the fix — no I/O, no clock read, no storage
import — so the policy is trivially unit-testable and the store
(``experience.memory_store``) can call it while holding its own lock:

* :func:`compute_score` — an **explainable** weighted score
  (reuse × freshness × promoted), each component inspectable via
  :class:`MemoryHealth.components`. This is a ranking heuristic, **not** a
  retrieval algorithm, and it does not change any recall semantics.
* :func:`freshness` — exponential time decay (``0.5 ** (age / half_life)``).
* :func:`should_archive` — archive **threshold** decision. ``min_score=0`` (the
  default) means *never archive*, so the default configuration changes nothing.
* :func:`is_archived` — read an entry's archive flag (field or metadata).

Honesty note (INC9 §2.2.6): archiving is a **marker, never a delete**, and the
target store is the **in-process** ``memory_store._PER_TENANT`` — so archiving
holds for the life of the process and does **not** survive a restart (there is
no ``memory_archive`` table; persistence was explicitly deferred, open question
O2). The pgvector ``/memory/store`` / ``/memory/search`` path is untouched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "MemoryHealth",
    "compute_score",
    "freshness",
    "should_archive",
    "is_archived",
]


@dataclass(frozen=True)
class MemoryHealth:
    """The explainable health of one memory entry (INC9 §4.1)."""

    score: float
    reuse_count: int
    age_days: float
    promoted: bool
    archived: bool
    #: Per-term breakdown of ``score`` (``reuse`` / ``fresh`` / ``promote``),
    #: so a reader can see *why* the number is what it is.
    components: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 6),
            "reuse_count": self.reuse_count,
            "age_days": round(self.age_days, 6),
            "promoted": self.promoted,
            "archived": self.archived,
            "components": {k: round(v, 6) for k, v in self.components.items()},
        }


def freshness(age_days: float, *, half_life_days: float = 30.0) -> float:
    """Time-decay weight in ``(0, 1]``: ``1.0`` fresh, halving per half-life.

    Pure and monotonic-decreasing in ``age_days``. A non-positive age is treated
    as brand-new (``1.0``); a non-positive half-life degrades to a step function
    (``1.0`` at age ``0``, else ``0.0``) rather than dividing by zero.
    """
    try:
        age = float(age_days)
    except (TypeError, ValueError):
        age = 0.0
    age = max(0.0, age)
    try:
        half_life = float(half_life_days)
    except (TypeError, ValueError):
        half_life = 0.0
    if half_life <= 0.0:
        return 1.0 if age <= 0.0 else 0.0
    return 0.5 ** (age / half_life)


def compute_score(
    *,
    reuse_count: int,
    age_days: float,
    promoted: bool,
    w_reuse: float = 0.5,
    w_fresh: float = 0.3,
    w_promote: float = 0.2,
    reuse_cap: int = 10,
    half_life_days: float = 30.0,
) -> float:
    """Explainable weighted memory score in ``[0, 1]``.

    ``score = w_reuse·min(reuse, cap)/cap + w_fresh·freshness(age)
    + w_promote·[promoted]``, clamped to ``[0, 1]``. ``reuse_cap`` keeps a
    single hot entry from dominating by reuse alone.
    """
    try:
        reuse = max(0, int(reuse_count))
    except (TypeError, ValueError):
        reuse = 0
    try:
        cap = max(1, int(reuse_cap))
    except (TypeError, ValueError):
        cap = 1

    reuse_term = float(w_reuse) * (min(reuse, cap) / cap)
    fresh_term = float(w_fresh) * freshness(age_days, half_life_days=half_life_days)
    promote_term = float(w_promote) if promoted else 0.0

    return max(0.0, min(1.0, reuse_term + fresh_term + promote_term))


def should_archive(
    health: MemoryHealth,
    *,
    min_score: float,
    max_age_days: float = 0.0,
) -> bool:
    """Whether ``health`` should be archived.

    Archives when the score is **below** ``min_score`` (``min_score=0`` ⇒ never,
    the default ⇒ behaviour unchanged) **or** the entry is older than
    ``max_age_days`` (``max_age_days=0`` ⇒ no age rule, the default).
    """
    try:
        floor = float(min_score)
    except (TypeError, ValueError):
        floor = 0.0
    try:
        age_limit = float(max_age_days)
    except (TypeError, ValueError):
        age_limit = 0.0

    if floor > 0.0 and health.score < floor:
        return True
    if age_limit > 0.0 and health.age_days > age_limit:
        return True
    return False


def is_archived(entry: Any) -> bool:
    """Whether ``entry`` is archived — from its ``archived`` field or metadata.

    Duck-typed so it works on ``MemoryEntry`` or a plain mapping. The field wins
    when it is a real ``bool``; otherwise ``metadata["archived"]`` is consulted
    (that is the fallback the store also writes, so the two never disagree).
    """
    flag = getattr(entry, "archived", None)
    if isinstance(flag, bool):
        return flag
    metadata = getattr(entry, "metadata", None)
    if isinstance(metadata, dict):
        return bool(metadata.get("archived"))
    if isinstance(entry, dict):
        if isinstance(entry.get("archived"), bool):
            return bool(entry["archived"])
        return bool((entry.get("metadata") or {}).get("archived"))
    return False
