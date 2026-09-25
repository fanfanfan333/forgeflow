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
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from forgeflow.config import get_settings
from forgeflow.repositories.base import new_id, scope_key, utcnow

logger = logging.getLogger(__name__)

__all__ = [
    "ContextBuildStat",
    "get_build_stats",
    "persist_context_build",
    "read_build_stats_pg",
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
    #: INC8 §3.2 (additive): the skill refs this build selected. Column added by
    #: migration ``012``; defaults to empty so pre-INC8 rows stay valid.
    skill_refs: list[str] = field(default_factory=list)
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
            "skill_refs": list(self.skill_refs),
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


def record_context_build(
    bundle: Any,
    *,
    tenant_id: str | None = None,
    run_id: str | None = None,
    skill_refs: list[str] | None = None,
) -> ContextBuildStat:
    """Snapshot a ``ContextBundle`` into a stat row and fold it into the totals.

    ``skill_refs`` defaults to the bundle's own skill sections (``source ==
    "skill"``), so the per-task skill signal INC8 metric #9 needs is captured
    automatically without the caller changing.
    """
    sections = list(getattr(bundle, "sections", []) or [])
    tokens_raw = int(getattr(bundle, "tokens_raw", 0) or 0)
    tokens_used = int(getattr(bundle, "tokens_used", 0) or 0)
    recalled = int(getattr(bundle, "recalled", 0) or 0)
    if skill_refs is None:
        skill_refs = [
            str(section.get("ref_id"))
            for section in sections
            if isinstance(section, dict) and section.get("source") == "skill"
        ]

    entry = ContextBuildStat(
        tenant_id=tenant_id,
        run_id=run_id,
        tokens_raw=tokens_raw,
        tokens_used=tokens_used,
        compression_ratio=float(getattr(bundle, "compression_ratio", 1.0) or 1.0),
        hit_rate=float(getattr(bundle, "hit_rate", 0.0) or 0.0),
        skill_refs=list(skill_refs),
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
                   compression_ratio, hit_rate, skill_refs, created_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                """,
                entry.id,
                scope_key(entry.tenant_id, get_settings().default_tenant_id),
                # ``context_build_stats.run_id`` is opaque TEXT (014): the hub run
                # id is written verbatim, never coerced to NULL.
                entry.run_id or None,
                entry.tokens_raw,
                entry.tokens_used,
                entry.compression_ratio,
                entry.hit_rate,
                list(entry.skill_refs),
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


async def read_build_stats_pg(
    tenant_id: str | None, *, limit: int = 20
) -> dict[str, Any]:
    """Read + aggregate ``context_build_stats`` for ``tenant_id`` (postgres).

    The aggregation happens **in SQL** (``COUNT``/``SUM``/``AVG``) rather than by
    pulling rows back to sum them in Python; ``recent`` is only the newest
    ``limit`` rows, ordered ``created_at DESC``.

    Tenant filter: ``context_build_stats.tenant_id`` is opaque ``TEXT`` (since
    migration 013) and :func:`persist_context_build` writes the non-null
    ``scope_key`` (``None`` → ``Settings.default_tenant_id``). The read therefore
    filters with ``tenant_id = $1`` on the same key — the literal string round
    trips and there is no shared ``NULL`` bucket to fall back to. (Rows written
    by the retired UUID-coercion path had ``tenant_id IS NULL``; a fresh
    database has none, and such orphans are intentionally not matched.)

    Returns the same shape as :func:`get_build_stats`. A real DB error is **not**
    swallowed here: it propagates so the caller
    (:mod:`forgeflow.api.routers.context`) can report an explicit ``degraded``
    response instead of mixing in-process numbers with database numbers.
    """
    from forgeflow.database import get_pool

    pool = await get_pool()

    where = "tenant_id = $1"
    params: list[Any] = [scope_key(tenant_id, get_settings().default_tenant_id)]

    limit_idx = len(params) + 1
    async with pool.acquire() as conn:
        agg = await conn.fetchrow(
            f"""
            SELECT
                COUNT(*)                      AS builds,
                COALESCE(SUM(tokens_raw), 0)  AS tokens_raw,
                COALESCE(SUM(tokens_used), 0) AS tokens_used,
                AVG(compression_ratio)        AS compression_ratio,
                AVG(hit_rate)                 AS hit_rate
            FROM context_build_stats
            WHERE {where}
            """,  # noqa: S608 — `where` is a fixed literal, never user input
            *params,
        )
        rows = await conn.fetch(
            f"""
            SELECT id, tenant_id, run_id, tokens_raw, tokens_used,
                   compression_ratio, hit_rate, created_at
            FROM context_build_stats
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT ${limit_idx}
            """,  # noqa: S608 — `where` is a fixed literal, never user input
            *params,
            int(limit),
        )

    builds = int(agg["builds"] or 0) if agg is not None else 0
    tokens_raw = int(agg["tokens_raw"] or 0) if agg is not None else 0
    tokens_used = int(agg["tokens_used"] or 0) if agg is not None else 0
    raw_ratio = agg["compression_ratio"] if agg is not None else None
    raw_hit = agg["hit_rate"] if agg is not None else None

    recent = [
        {
            "id": str(row["id"]),
            "tenant_id": str(row["tenant_id"]) if row["tenant_id"] is not None else None,
            "run_id": str(row["run_id"]) if row["run_id"] is not None else None,
            "tokens_raw": int(row["tokens_raw"] or 0),
            "tokens_used": int(row["tokens_used"] or 0),
            "compression_ratio": (
                round(float(row["compression_ratio"]), 4)
                if row["compression_ratio"] is not None
                else None
            ),
            "hit_rate": (
                round(float(row["hit_rate"]), 4) if row["hit_rate"] is not None else None
            ),
            "created_at": (
                row["created_at"].isoformat() if row["created_at"] is not None else None
            ),
        }
        for row in rows
    ]

    return {
        "builds": builds,
        "tokens_raw": tokens_raw,
        "tokens_used": tokens_used,
        "compression_ratio": float(raw_ratio) if raw_ratio is not None else None,
        "hit_rate": float(raw_hit) if raw_hit is not None else None,
        "has_data": builds > 0,
        "recent": recent,
    }
