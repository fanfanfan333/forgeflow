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
) -> MemoryEntry:
    """Persist a scoped memory, annotating its namespace."""
    scope_value = scope if is_valid_scope(scope) else MemoryScope.SEMANTIC.value
    entry = MemoryEntry(
        tenant_id=tenant_id,
        scope=scope_value,
        content=content,
        team_id=team_id,
        namespace=scope_namespace(scope_value, tenant_id, actor_id=actor_id, team_id=team_id),
        metadata=dict(metadata or {}),
        embedding=embed_text(content),
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
) -> list[MemoryEntry]:
    rows = list(_PER_TENANT.get(tenant_id or "global", []))
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
) -> list[tuple[MemoryEntry, float]]:
    """Semantic search over the scope-partitioned store (INC2 D1, §2.17).

    Uses the dependency-free ``experience.embedding.embed_text`` cosine
    similarity, so it returns results **without PostgreSQL or an OpenAI key**
    (the mock embedding provider yields a deterministic vector). Returns
    ``(entry, similarity)`` pairs sorted by descending similarity.
    """
    rows = list(_PER_TENANT.get(tenant_id or "global", []))
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
