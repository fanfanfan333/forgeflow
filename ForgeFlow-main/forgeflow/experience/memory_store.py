"""In-process five-layer Memory store (P0-13 skeleton).

Provides a scope-partitioned memory store that works with no PostgreSQL so the
Memory Hub and its API are demonstrable offline. The legacy
``/memory/store`` / ``/memory/search`` endpoints keep using pgvector; this store
is the hub-level, scope-aware view layered on top.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from forgeflow.experience.embedding import cosine_similarity, embed_text
from forgeflow.experience.memory_types import default_type_for_scope, is_valid_type
from forgeflow.experience.scopes import MemoryScope, is_valid_scope, scope_namespace
from forgeflow.repositories.base import new_id, utcnow

_PER_TENANT: dict[str, list["MemoryEntry"]] = {}
_LOCK = asyncio.Lock()


@dataclass
class MemoryEntry:
    """One memory fact in a given scope layer."""

    id: str = field(default_factory=new_id)
    tenant_id: str | None = None
    scope: str = MemoryScope.SEMANTIC.value
    content: str = ""
    team_id: str | None = None
    namespace: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] | None = None
    #: INC9 B2 — how many times recall actually selected this entry
    #: (``context_builder`` bumps it). Default 0 ⇒ score's reuse term is 0 and
    #: every existing construction site is unchanged.
    reuse_count: int = 0
    #: INC9 B2 — archive **marker** (never a delete). Default False, so the read
    #: paths (which exclude archived by default) are unchanged.
    archived: bool = False
    #: INC9 B3 — the *type* dimension of the two-dimensional memory model
    #: (working / episodic / semantic / procedural). Empty ⇒ normalised from the
    #: scope by ``save_memory`` (fail-safe, so existing callers are unchanged).
    memory_type: str = ""
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        return data


def clear_memory_entries() -> None:
    """Reset the store. Test helper only."""
    _PER_TENANT.clear()


async def save_memory(
    tenant_id: str | None,
    scope: str,
    content: str,
    *,
    team_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    actor_id: str | None = None,
    memory_type: str | None = None,
) -> MemoryEntry:
    """Persist a scoped memory, annotating its namespace and type.

    ``memory_type`` is validated and **fail-safe normalised** (an unknown value
    falls back to :func:`default_type_for_scope`) rather than raising, so no
    existing caller changes behaviour (INC9 §B3).
    """
    scope_value = scope if is_valid_scope(scope) else MemoryScope.SEMANTIC.value
    type_value = (
        memory_type
        if memory_type and is_valid_type(memory_type)
        else default_type_for_scope(scope_value)
    )
    entry = MemoryEntry(
        tenant_id=tenant_id,
        scope=scope_value,
        content=content,
        team_id=team_id,
        namespace=scope_namespace(scope_value, tenant_id, actor_id=actor_id, team_id=team_id),
        metadata=dict(metadata or {}),
        embedding=embed_text(content),
        memory_type=type_value,
    )
    async with _LOCK:
        _PER_TENANT.setdefault(tenant_id or "global", []).append(entry)
    return entry


async def list_memories(
    tenant_id: str | None,
    *,
    scope: str | None = None,
    team_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    include_archived: bool = False,
) -> list[MemoryEntry]:
    rows = list(_PER_TENANT.get(tenant_id or "global", []))
    if not include_archived:
        rows = [r for r in rows if not r.archived]
    if scope:
        rows = [r for r in rows if r.scope == scope]
    if team_id:
        rows = [r for r in rows if r.team_id == team_id]
    rows.sort(key=lambda r: r.created_at, reverse=True)
    return rows[offset : offset + limit]


async def count_memories(tenant_id: str | None) -> int:
    return len(_PER_TENANT.get(tenant_id or "global", []))


async def get_memory(
    tenant_id: str | None, memory_id: str
) -> MemoryEntry | None:
    """Fetch one memory entry by id, tenant-scoped. ``None`` when absent."""
    for entry in _PER_TENANT.get(tenant_id or "global", []):
        if entry.id == memory_id:
            return entry
    return None


async def search(
    tenant_id: str | None,
    q: str,
    k: int = 5,
    *,
    scope: str | None = None,
    team_id: str | None = None,
    include_archived: bool = False,
) -> list[tuple[MemoryEntry, float]]:
    """Semantic search over the scope-partitioned store (INC2 D1, §2.17).

    Uses the dependency-free ``experience.embedding.embed_text`` cosine
    similarity, so it returns results **without PostgreSQL or an OpenAI key**
    (the mock embedding provider yields a deterministic vector). Returns
    ``(entry, similarity)`` pairs sorted by descending similarity.

    INC9 B2: ``include_archived`` defaults to ``False`` ⇒ archived entries are
    excluded. Because nothing is archived under the default config, the result is
    identical to before.
    """
    rows = list(_PER_TENANT.get(tenant_id or "global", []))
    if not include_archived:
        rows = [r for r in rows if not r.archived]
    if scope:
        rows = [r for r in rows if r.scope == scope]
    if team_id:
        rows = [r for r in rows if r.team_id == team_id]

    query_vec = embed_text(q)
    scored: list[tuple[MemoryEntry, float]] = []
    for entry in rows:
        vector = entry.embedding or embed_text(entry.content)
        scored.append((entry, float(cosine_similarity(query_vec, vector))))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[: max(0, k)]


async def move_memory(
    tenant_id: str | None,
    memory_id: str,
    to_scope: str,
    *,
    team_id: str | None = None,
    actor_id: str | None = None,
) -> MemoryEntry | None:
    """Re-scope a memory and recompute its namespace (INC2 A6, §2.6).

    Returns the updated entry, or ``None`` when no entry matches ``memory_id``.
    An unknown ``to_scope`` degrades to ``semantic`` via the same validation
    used by ``save_memory``.
    """
    scope_value = to_scope if is_valid_scope(to_scope) else MemoryScope.SEMANTIC.value
    async with _LOCK:
        for entry in _PER_TENANT.get(tenant_id or "global", []):
            if entry.id != memory_id:
                continue
            entry.scope = scope_value
            if team_id is not None:
                entry.team_id = team_id
            entry.namespace = scope_namespace(
                scope_value, tenant_id, actor_id=actor_id, team_id=entry.team_id
            )
            return entry
    return None


# --------------------------------------------------------------------------- #
# INC9 B2 — Memory lifecycle: reuse / archive / sweep                          #
# The read paths (list/search) already exclude archived entries by default;    #
# these are the write/produce counterparts. Everything is a *marker* — nothing #
# is ever physically deleted (INC9 §2.2.6), and archiving lives in the         #
# in-process store, so it does not survive a restart (no persistence table).   #
# --------------------------------------------------------------------------- #

async def mark_reused(tenant_id: str | None, memory_ids: list[str] | None) -> int:
    """Bump ``reuse_count`` for every entry in ``memory_ids``. Returns the count.

    The single runtime source of the score's reuse signal: ``context_builder``
    calls this for the memory sections it actually selected. Additive — a caller
    that never calls it leaves ``reuse_count`` at 0 (the honest "no reuse data"
    state, not a fabricated signal).
    """
    ids = {m for m in (memory_ids or []) if m}
    if not ids:
        return 0
    bumped = 0
    async with _LOCK:
        for entry in _PER_TENANT.get(tenant_id or "global", []):
            if entry.id in ids:
                entry.reuse_count += 1
                bumped += 1
    return bumped


def _mark_archived(entry: MemoryEntry, *, reason: str) -> None:
    """Flag ``entry`` archived + stamp metadata (never deletes). Caller locks."""
    entry.archived = True
    meta = dict(entry.metadata or {})
    meta["archived"] = True
    meta.setdefault("archived_at", utcnow().isoformat())
    if reason:
        meta["archive_reason"] = reason
    entry.metadata = meta


async def archive_memory(
    tenant_id: str | None, memory_id: str, *, reason: str = ""
) -> MemoryEntry | None:
    """Archive one memory (marker only). ``None`` when the id is not present."""
    async with _LOCK:
        for entry in _PER_TENANT.get(tenant_id or "global", []):
            if entry.id == memory_id:
                _mark_archived(entry, reason=reason)
                return entry
    return None


def _entry_health(entry: MemoryEntry, now: datetime, settings: Any, decay_enabled: bool) -> Any:
    """Build a :class:`MemoryHealth` for one entry (pure; no lock needed)."""
    from forgeflow.experience.lifecycle import MemoryHealth, compute_score, freshness

    age_days = max(0.0, (now - entry.created_at).total_seconds() / 86400.0)
    # When decay is disabled the freshness term is frozen at "brand new" so the
    # score carries no time signal (matches the master switch's intent).
    effective_age = age_days if decay_enabled else 0.0
    promoted = entry.scope in (MemoryScope.TEAM.value, MemoryScope.ORG.value)
    half_life = float(getattr(settings, "memory_decay_half_life_days", 30.0) or 30.0)
    w_reuse = float(getattr(settings, "memory_score_w_reuse", 0.5) or 0.0)
    w_fresh = float(getattr(settings, "memory_score_w_fresh", 0.3) or 0.0)
    w_promote = float(getattr(settings, "memory_score_w_promote", 0.2) or 0.0)
    reuse = max(0, int(entry.reuse_count))
    cap = 10
    components = {
        "reuse": w_reuse * (min(reuse, cap) / cap),
        "fresh": w_fresh * freshness(effective_age, half_life_days=half_life),
        "promote": w_promote if promoted else 0.0,
    }
    score = compute_score(
        reuse_count=reuse,
        age_days=effective_age,
        promoted=promoted,
        w_reuse=w_reuse,
        w_fresh=w_fresh,
        w_promote=w_promote,
        half_life_days=half_life,
    )
    return MemoryHealth(
        score=score,
        reuse_count=reuse,
        age_days=age_days,
        promoted=promoted,
        archived=entry.archived,
        components=components,
    )


async def lifecycle_sweep(tenant_id: str | None, *, now: datetime | None = None) -> dict[str, Any]:
    """One score/decay/archive pass over the tenant's in-process memories.

    Gated by ``Settings.memory_decay_enabled`` (default ``False`` ⇒ an explicit
    no-op, so the default configuration changes nothing). Archives are decided by
    :func:`forgeflow.experience.lifecycle.should_archive`, whose default
    thresholds (``min_score=0`` / ``max_age_days=0``) mean **never archive**.
    Idempotent: a second run re-processes the same active set and archives
    nothing new for already-archived entries (they are counted as ``skipped``).
    """
    from forgeflow.config import get_settings
    from forgeflow.experience.lifecycle import should_archive

    settings = get_settings()
    enabled = bool(getattr(settings, "memory_decay_enabled", False))
    rows = list(_PER_TENANT.get(tenant_id or "global", []))

    if not enabled:
        return {"enabled": False, "scored": 0, "archived": 0, "skipped": len(rows)}

    moment = now or utcnow()
    min_score = float(getattr(settings, "memory_archive_min_score", 0.0) or 0.0)
    max_age = float(getattr(settings, "memory_archive_max_age_days", 0.0) or 0.0)

    scored = archived = skipped = 0
    async with _LOCK:
        for entry in rows:
            if entry.archived:
                skipped += 1
                continue
            health = _entry_health(entry, moment, settings, enabled)
            scored += 1
            if should_archive(health, min_score=min_score, max_age_days=max_age):
                _mark_archived(entry, reason="lifecycle_sweep")
                archived += 1
    return {"enabled": True, "scored": scored, "archived": archived, "skipped": skipped}


async def lifecycle_summary(tenant_id: str | None, *, now: datetime | None = None) -> dict[str, Any]:
    """Read-only lifecycle health: active/archived counts + mean score.

    Always safe to call (no mutation). ``avg_score`` is ``None`` when there are no
    active entries — "no data", never a fabricated ``0``.
    """
    from forgeflow.config import get_settings

    settings = get_settings()
    enabled = bool(getattr(settings, "memory_decay_enabled", False))
    moment = now or utcnow()
    rows = list(_PER_TENANT.get(tenant_id or "global", []))
    active = [r for r in rows if not r.archived]
    archived = [r for r in rows if r.archived]
    scores = [_entry_health(r, moment, settings, enabled).score for r in active]
    return {
        "total": len(rows),
        "active": len(active),
        "archived": len(archived),
        "avg_score": (sum(scores) / len(scores)) if scores else None,
        "decay_enabled": enabled,
    }
