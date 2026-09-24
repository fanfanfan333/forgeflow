"""Context Builder observability route (INC2-10) — the **read** side of
``context_build_stats``.

Before this router the platform only ever *wrote* per-build stats
(:func:`forgeflow.observability.context_stats.persist_context_build`); nothing
read the table back. ``ROUTE_PERMISSION_MAP`` meanwhile advertised
``("GET", "/context")`` while no route served it — a *phantom* entry that
promised a capability the platform did not have. This router closes that loop
and is deliberately the only new API surface in this increment.

Honesty rule (hard requirement)
-------------------------------
The two backends report two different, non-interchangeable truths:

  * **offline (memory) profile** → the in-process aggregate
    (:func:`~forgeflow.observability.context_stats.get_build_stats`),
    ``source="memory"``, ``degraded=False``.
  * **postgres profile** → the DB aggregate
    (:func:`~forgeflow.observability.context_stats.read_build_stats_pg`),
    ``source="postgres"``, ``degraded=False`` on success.

A postgres read failure is reported **explicitly** (``degraded=True`` +
``error`` + ``has_data=False``). It must **never** silently fall back to the
in-process numbers — mixing two source-of-truths would be a *second* fail-open
defect in a repo that already has one on record.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.hub_schemas import ContextStatsResponse
from forgeflow.config import get_settings
from forgeflow.observability.context_stats import get_build_stats, read_build_stats_pg

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("", response_model=ContextStatsResponse)
async def context_stats(tenant: str = Depends(resolve_tenant)) -> ContextStatsResponse:
    """Context-build aggregate for the ops page (memory or postgres source)."""
    # Offline profile: the in-process roll-up is the *only* truth available.
    if get_settings().storage_backend.lower() != "postgres":
        return ContextStatsResponse(
            source="memory", degraded=False, error=None, **get_build_stats()
        )

    # PostgreSQL profile: read the durable table. A failure is surfaced as an
    # explicit degradation — never a quiet fallback to the memory aggregate.
    try:
        payload = await read_build_stats_pg(tenant)
    except Exception as exc:  # noqa: BLE001 — degrade explicitly, never 500
        logger.warning("context stats: PostgreSQL read unavailable: %s", exc)
        return ContextStatsResponse(
            builds=0,
            tokens_raw=0,
            tokens_used=0,
            compression_ratio=None,
            hit_rate=None,
            has_data=False,
            recent=[],
            source="postgres",
            degraded=True,
            error=str(exc),
        )

    return ContextStatsResponse(source="postgres", degraded=False, error=None, **payload)
