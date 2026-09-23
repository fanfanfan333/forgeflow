"""INC2-10 — observability for the Context Builder (A5).

``build_context`` already computes ``compression_ratio`` / ``hit_rate``; until
now those numbers died inside the returned bundle. This module turns them into
something the ops page can read: an in-process aggregate (works offline, zero
dependency) plus an optional durable row in ``context_build_stats`` when the
postgres backend is active.

Offline contract: recording is always safe. Persisting is best-effort and never
raises — a missing DB must not fail a run.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from forgeflow.config import get_settings
from forgeflow.repositories.base import new_id, utcnow

logger = logging.getLogger(__name__)

__all__ = [
    "ContextBuildStat",
    "get_build_stats",
    "persist_context_build",
    "record_context_build",
    "reset_build_stats",
]


@dataclass
class ContextBuildStat:
    """One context-build measurement (mirrors the ``context_build_stats`` row)."""

    id: str = field(default_factory=new_id)
    tenant_id: str | None = None
    run_id: str | None = None
    tokens_raw: int = 0
    tokens_used: int = 0
    compression_ratio: float = 1.0
    hit_rate: float = 0.0
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "run_id": self.run_id,
            "tokens_raw": self.tokens_raw,
            "tokens_used": self.tokens_used,
            "compression_ratio": round(self.compression_ratio, 4),
            "hit_rate": round(self.hit_rate, 4),
            "created_at": self.created_at.isoformat(),
        }


# In-process roll-up. Separate from context_builder's own counters so the ops
# view is not polluted by direct (test) calls to build_context.
_TOTALS: dict[str, float] = {
    "builds": 0.0,
    "tokens_raw": 0.0,
    "tokens_used": 0.0,
    "sections": 0.0,
    "recalled": 0.0,
}
_RECENT: list[ContextBuildStat] = []
_RECENT_LIMIT = 100


def _as_uuid(value: str | None) -> str | None:
    """Non-UUID ids (the ``"default"`` sentinel) become NULL, never an error."""
    if not value:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return None


def record_context_build(
    bundle: Any,
    *,
    tenant_id: str | None = None,
    run_id: str | None = None,
) -> ContextBuildStat:
    """Snapshot a ``ContextBundle`` into a stat row and fold it into the totals."""
    sections = list(getattr(bundle, "sections", []) or [])
    tokens_raw = int(getattr(bundle, "tokens_raw", 0) or 0)
    tokens_used = int(getattr(bundle, "tokens_used", 0) or 0)
    recalled = int(getattr(bundle, "recalled", 0) or 0)

    entry = ContextBuildStat(
        tenant_id=tenant_id,
        run_id=run_id,
        tokens_raw=tokens_raw,
        tokens_used=tokens_used,
        compression_ratio=float(getattr(bundle, "compression_ratio", 1.0) or 1.0),
        hit_rate=float(getattr(bundle, "hit_rate", 0.0) or 0.0),
    )

    _TOTALS["builds"] += 1
    _TOTALS["tokens_raw"] += tokens_raw
    _TOTALS["tokens_used"] += tokens_used
    _TOTALS["sections"] += len(sections)
    _TOTALS["recalled"] += recalled

    _RECENT.append(entry)
    if len(_RECENT) > _RECENT_LIMIT:
        del _RECENT[0 : len(_RECENT) - _RECENT_LIMIT]
    return entry


async def persist_context_build(entry: ContextBuildStat) -> bool:
    """Write the row to ``context_build_stats`` when running on postgres.

    Returns ``True`` only when a row was actually written. On the memory
    backend (or any DB failure) this is a no-op returning ``False``.
    """
    if get_settings().storage_backend.lower() != "postgres":
        return False
    try:
        from forgeflow.database import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO context_build_stats
                  (id, tenant_id, run_id, tokens_raw, tokens_used,
                   compression_ratio, hit_rate, created_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                """,
                _as_uuid(entry.id) or entry.id,
                _as_uuid(entry.tenant_id),
                _as_uuid(entry.run_id),
                entry.tokens_raw,
                entry.tokens_used,
                entry.compression_ratio,
                entry.hit_rate,
                entry.created_at,
            )
        return True
    except Exception as exc:  # noqa: BLE001 — observability must never break a run
        logger.debug("context_build_stats write skipped: %s", exc)
        return False


def get_build_stats() -> dict[str, Any]:
    """Aggregate view for the ops page — honest ``—`` when there is no data."""
    builds = _TOTALS["builds"]
    raw = _TOTALS["tokens_raw"]
    used = _TOTALS["tokens_used"]
    recalled = _TOTALS["recalled"]
    return {
        "builds": int(builds),
        "tokens_raw": int(raw),
        "tokens_used": int(used),
        "compression_ratio": (used / raw) if raw > 0 else None,
        "hit_rate": (_TOTALS["sections"] / recalled) if recalled > 0 else None,
        "has_data": builds > 0,
        "recent": [e.to_dict() for e in reversed(_RECENT[-20:])],
    }


def reset_build_stats() -> None:
    """Test helper — zero the counters and drop the recent window."""
    for key in _TOTALS:
        _TOTALS[key] = 0.0
    _RECENT.clear()
