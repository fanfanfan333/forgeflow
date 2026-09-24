"""Memory *type* dimension — the second axis of the two-dimensional memory model.

INC9 §B3 (docs/sop/12-INC9-DESIGN.md §2.3, review findings ①+⑨). The existing
``experience.scopes`` mixes two different ideas in one five-value enum: an
**ownership** axis (``user`` / ``team`` / ``org``) and a **type** axis
(``episodic`` / ``semantic``). That is exactly the boundary blur review ①
flagged.

This module adds the missing **type** axis — ``working → episodic → semantic →
procedural`` — as a pure, additive tag:

* :class:`MemoryType` — the four types.
* :func:`default_type_for_scope` — a backwards-compatible *derivation* of a
  type from an existing ownership scope, so no stored data has to change.
* :func:`next_type` — the ladder transition (``working → episodic → semantic →
  procedural``) used when a memory is promoted.

The old five-value ``MemoryScope`` enum is **kept verbatim** (it is consumed by
``SCOPE_RULES`` and existing tests); the type axis is layered on top as a label,
never as a structural change. Pure module: stdlib only, no I/O.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "MemoryType",
    "MEMORY_TYPES",
    "TYPE_LADDER",
    "default_type_for_scope",
    "is_valid_type",
    "next_type",
]


class MemoryType(str, Enum):
    """The type dimension of the two-dimensional memory model."""

    WORKING = "working"  # short-lived task/session context
    EPISODIC = "episodic"  # a single run's event trace
    SEMANTIC = "semantic"  # distilled, stable facts
    PROCEDURAL = "procedural"  # reusable procedures / skills (skill-side memory)


#: All type values, in ladder order.
MEMORY_TYPES: tuple[str, ...] = tuple(t.value for t in MemoryType)

#: The promotion ladder: each promote moves one rung up (capped at the top).
TYPE_LADDER: tuple[str, ...] = MEMORY_TYPES

#: Ownership scope → default type. This is a **backwards-compatible derivation**
#: (the existing scope enum is unchanged); the mapping mirrors the intent of
#: ``experience.scopes.SCOPE_RULES`` (episodic≈episodic, org≈distilled fact).
_SCOPE_DEFAULT_TYPE: dict[str, str] = {
    "user": MemoryType.WORKING.value,
    "team": MemoryType.WORKING.value,
    "episodic": MemoryType.EPISODIC.value,
    "semantic": MemoryType.SEMANTIC.value,
    "org": MemoryType.SEMANTIC.value,
}


def default_type_for_scope(scope: str) -> str:
    """Default :class:`MemoryType` value for an ownership ``scope``.

    Unknown scopes derive ``working`` (the most ephemeral type) — fail-safe, so
    a caller with an unexpected scope still gets a valid type.
    """
    return _SCOPE_DEFAULT_TYPE.get(str(scope), MemoryType.WORKING.value)


def is_valid_type(t: str) -> bool:
    """Whether ``t`` is one of the four valid type values."""
    return str(t) in MEMORY_TYPES


def next_type(current: str, event: str) -> str:
    """The type after ``event``.

    ``"promote"`` advances one rung up the ladder (``working → episodic →
    semantic → procedural``, capped at ``procedural``); ``"demote"`` steps one
    rung down (floored at ``working``). Any other event is a no-op. An invalid
    ``current`` is normalised to ``working`` first (fail-safe).
    """
    cur = current if is_valid_type(current) else MemoryType.WORKING.value
    idx = TYPE_LADDER.index(cur)
    if event == "promote":
        return TYPE_LADDER[min(idx + 1, len(TYPE_LADDER) - 1)]
    if event == "demote":
        return TYPE_LADDER[max(idx - 1, 0)]
    return cur
