"""In-memory Experience repository.

Storage layout (docs §3.3):
  * ``_STORE[tenant_key][experience_id] -> ExperienceRecord`` — tenant-partitioned
  * ``_LINKS[tenant_key][(experience_id, memory_id)] -> relation`` — N:M links

**Value semantics (INC2-16 fix).** ``save`` stores a deep copy of the record and
every read (``get`` / ``list`` / ``find_similar`` / ``all_records``) returns a
fresh copy. A stored row therefore never aliases a caller's object, so mutations
to a record returned earlier cannot silently leak into storage (and vice-versa) —
exactly the failure mode the reference-based store hid, and the reason the
``merged_from`` / ``conflict_with`` / ``dedup_key`` / ``confidence`` columns did
not truly round-trip before. All four fields are persisted explicitly.

Similarity uses a hand-written pure-Python cosine (no numpy). When an
experience has no embedding we degrade to tag overlap so the closed loop still
works offline (docs R6).
"""

from __future__ import annotations

import asyncio
import copy
import math
from typing import Any

from forgeflow.experience.models import ExperienceRecord
from forgeflow.repositories.base import TenantScopedRepository

# Module-level singletons so every factory call shares one store (docs §3.3).
_STORE: dict[str, dict[str, ExperienceRecord]] = {}
_LINKS: dict[str, dict[tuple[str, str], str]] = {}
_LOCK = asyncio.Lock()


def clear_memory_store() -> None:
    """Reset all in-memory state. Test helper only."""
    _STORE.clear()
    _LINKS.clear()


def _clone(record: ExperienceRecord) -> ExperienceRecord:
    """Return a deep copy so storage never aliases a caller-owned instance."""
    return copy.deepcopy(record)


def _cosine(a: list[float] | None, b: list[float] | None) -> float:
    """Cosine similarity in [0,1]; 0.0 when either vector is missing/degenerate."""
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = 0.0
    na = 0.0
    nb = 0.0
    for i in range(n):
        x = float(a[i])
        y = float(b[i])
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _tag_overlap(query: list[str] | None, record: ExperienceRecord) -> float:
    """Jaccard-ish overlap between requested tags and a record's tags."""
    if not query or not record.tags:
        return 0.0
    q = {t.lower() for t in query}
    r = {t.lower() for t in record.tags}
    if not q or not r:
        return 0.0
    return len(q & r) / len(q | r)


class MemoryExperienceRepository(TenantScopedRepository):
    """Dict-backed ``ExperienceRepository`` (identical surface to the PG impl)."""

    async def save(self, record: ExperienceRecord) -> ExperienceRecord:
        key = self.scope_key(record.tenant_id)
        async with _LOCK:
            # Store a snapshot: later in-place edits of ``record`` must not leak
            # into storage. ``merged_from`` / ``conflict_with`` / ``dedup_key`` /
            # ``confidence`` are real fields, so they are captured here too.
            _STORE.setdefault(key, {})[record.id] = _clone(record)
        return record

    async def get(self, tenant_id: str | None, experience_id: str) -> ExperienceRecord | None:
        # INC43 §3.4 / BE-5 — fail-closed: an unresolved tenant reads NOTHING
        # (never the shared "default" bucket, never every tenant's rows).
        if not tenant_id:
            return None
        stored = _STORE.get(self.scope_key(tenant_id), {}).get(experience_id)
        return _clone(stored) if stored is not None else None

    async def list(
        self,
        tenant_id: str | None,
        *,
        outcome: str | None = None,
        tag: str | None = None,
        run_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ExperienceRecord]:
        if not tenant_id:  # BE-5 fail-closed (discovery returns empty, not all)
            return []
        rows = list(_STORE.get(self.scope_key(tenant_id), {}).values())
        if outcome:
            rows = [r for r in rows if r.outcome == outcome]
        if tag:
            rows = [r for r in rows if tag in r.tags]
        if run_id:
            rows = [r for r in rows if r.run_id == run_id]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return [_clone(r) for r in rows[offset : offset + limit]]

    async def find_similar(
        self,
        tenant_id: str | None,
        embedding: list[float] | None,
        *,
        k: int = 10,
        min_similarity: float = 0.85,
        tags: list[str] | None = None,
    ) -> list[tuple[ExperienceRecord, float]]:
        if not tenant_id:  # BE-5 fail-closed
            return []
        rows = list(_STORE.get(self.scope_key(tenant_id), {}).values())
        scored: list[tuple[ExperienceRecord, float]] = []
        for rec in rows:
            if embedding is not None:
                sim = _cosine(embedding, rec.embedding)
            else:
                sim = _tag_overlap(tags, rec)
            if sim >= min_similarity:
                scored.append((rec, sim))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return [(_clone(rec), sim) for rec, sim in scored[:k]]

    async def link_memory(
        self,
        tenant_id: str | None,
        experience_id: str,
        memory_id: str,
        relation: str = "source",
    ) -> None:
        # INC43 §3.4 / BE-5 — fail-closed write: an unresolved tenant links
        # nothing (never into the shared "default" bucket).
        if not tenant_id:
            return
        key = self.scope_key(tenant_id)
        async with _LOCK:
            _LINKS.setdefault(key, {})[(experience_id, memory_id)] = relation
            rec = _STORE.get(key, {}).get(experience_id)
            if rec is not None and memory_id not in rec.memory_ids:
                rec.memory_ids.append(memory_id)

    async def list_memories(self, tenant_id: str | None, experience_id: str) -> list[str]:
        if not tenant_id:  # BE-5 fail-closed
            return []
        key = self.scope_key(tenant_id)
        links = _LINKS.get(key, {})
        return [mem for (exp, mem) in links if exp == experience_id]

    async def count(self, tenant_id: str | None) -> int:
        if not tenant_id:  # BE-5 fail-closed: an unresolved tenant counts zero
            return 0
        return len(_STORE.get(self.scope_key(tenant_id), {}))

    def all_records(self, tenant_id: str | None) -> list[ExperienceRecord]:
        """Raw accessor used by the candidate compiler's clustering step."""
        if not tenant_id:  # BE-5 fail-closed: cluster over nothing, not everyone
            return []
        return [clone for clone in (
            _clone(r) for r in _STORE.get(self.scope_key(tenant_id), {}).values()
        )]
