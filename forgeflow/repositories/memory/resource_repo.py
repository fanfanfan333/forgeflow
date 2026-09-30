"""In-memory Resource Center repository (INC25 W1, offline profile).

Storage layout: ``_STORE[tenant_key][resource_id] -> ResourceRecord``.

Value semantics mirror ``repositories/memory/experience_repo.py``: ``save`` stores
a deep copy and every read returns a fresh copy, so a record a caller mutates
after saving can never leak into the store (and a stored record can never be
mutated through a returned reference).

The PG twin is ``repositories/postgres/resource_repo.py``; both implement
``repositories/base.py::ResourceRepository`` with identical signatures.
"""

from __future__ import annotations

import asyncio
import copy
from typing import Any

from forgeflow.repositories.base import TenantScopedRepository

# Module-level singleton so every factory call shares one store.
_STORE: dict[str, dict[str, Any]] = {}
_LOCK = asyncio.Lock()


def clear_resource_store() -> None:
    """Reset all in-memory resource state. Test helper only."""
    _STORE.clear()


def _clone(record: Any) -> Any:
    """Deep copy so the store never aliases a caller-owned instance."""
    return copy.deepcopy(record)


class MemoryResourceRepository(TenantScopedRepository):
    """Dict-backed ``ResourceRepository`` (identical surface to the PG impl)."""

    async def save(self, record: Any) -> Any:
        key = self.scope_key(getattr(record, "tenant_id", None))
        async with _LOCK:
            _STORE.setdefault(key, {})[record.id] = _clone(record)
        return record

    async def get(self, tenant_id: str | None, resource_id: str) -> Any | None:
        stored = _STORE.get(self.scope_key(tenant_id), {}).get(resource_id)
        return _clone(stored) if stored is not None else None

    async def list(
        self,
        tenant_id: str | None,
        *,
        kind: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Any]:
        rows = list(_STORE.get(self.scope_key(tenant_id), {}).values())
        if kind:
            rows = [r for r in rows if str(getattr(r, "kind", "")) == str(kind)]
        rows.sort(key=lambda r: getattr(r, "created_at", ""), reverse=True)
        return [_clone(r) for r in rows[offset : offset + limit]]

    async def delete(self, tenant_id: str | None, resource_id: str) -> None:
        key = self.scope_key(tenant_id)
        async with _LOCK:
            _STORE.get(key, {}).pop(resource_id, None)
