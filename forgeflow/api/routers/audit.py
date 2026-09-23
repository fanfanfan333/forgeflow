"""Audit-log search API — compliance + incident investigation.

Reads from the partitioned audit_log table populated by AuditMiddleware on
every request. Supports filtering by user, role, action (HTTP method),
resource (path), outcome, and a time window. Returns paginated results
sorted by most recent first.

This is the read path; the immutable write path is in middleware/audit.py.

Offline (memory) profile
------------------------
With no PostgreSQL the search/export paths must still answer 200 (never 500).
A bounded in-process **ring buffer** holds the most recent audit entries when no
pool is available; ``record_audit_entry()`` is the producer seam
(the persistent writer lives in ``middleware/audit.py``). Both
``/audit/search`` and ``/audit/export`` read from the same buffer, so the two
views are consistent by construction.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from forgeflow.api.dependencies import get_workspace_id

logger = logging.getLogger(__name__)
router = APIRouter()

# --------------------------------------------------------------------------- #
# Offline audit ring buffer                                                    #
# --------------------------------------------------------------------------- #

#: Max entries retained in the offline profile.
AUDIT_RING_CAPACITY = 5_000

#: The fields a search/export item exposes (shared by both backends).
_AUDIT_ITEM_FIELDS = (
    "id",
    "timestamp",
    "user_id",
    "role",
    "action",
    "resource",
    "resource_id",
    "outcome",
    "request_id",
    "metadata",
)

_RING: deque[dict[str, Any]] = deque(maxlen=AUDIT_RING_CAPACITY)


def record_audit_entry(entry: dict[str, Any]) -> None:
    """Append one audit entry to the offline ring buffer (bounded, newest last).

    The persistent producer is ``middleware/audit.py``; in the offline profile
    it (or a test) can call this to make ``/audit/search`` + ``/audit/export``
    return real data instead of failing on a missing pool.
    """
    normalised = {field: entry.get(field) for field in _AUDIT_ITEM_FIELDS}
    normalised["metadata"] = dict(entry.get("metadata") or {})
    normalised["workspace_id"] = entry.get("workspace_id")
    _RING.append(normalised)


def clear_audit_ring() -> None:
    """Drop all buffered entries. Test helper only."""
    _RING.clear()


def _parse_ts(value: Any) -> datetime | None:
    """Parse a datetime or ISO-8601 string into an aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _within_window(entry: dict[str, Any], since: datetime | None, until: datetime | None) -> bool:
    if since is None and until is None:
        return True
    moment = _parse_ts(entry.get("timestamp"))
    if moment is None:
        return False
    if since is not None and moment < since:
        return False
    if until is not None and moment >= until:
        return False
    return True


def _memory_filter(
    *,
    workspace_id: str | None = None,
    user_id: str | None = None,
    role: str | None = None,
    action: str | None = None,
    resource: str | None = None,
    outcome: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict[str, Any]]:
    """Filter the ring buffer the same way the PG path filters ``audit_log``.

    Tenant scoping mirrors :func:`search_audit_log`: an explicit ``workspace_id``
    matches only that tenant; otherwise only global (``NULL`` tenant) rows.
    """
    rows: list[dict[str, Any]] = []
    for entry in _RING:
        if workspace_id:
            if str(entry.get("workspace_id")) != str(workspace_id):
                continue
        elif entry.get("workspace_id") is not None:
            continue
        if user_id and str(entry.get("user_id")) != str(user_id):
            continue
        if role and entry.get("role") != role:
            continue
        if action and str(entry.get("action") or "").upper() != action.upper():
            continue
        if resource and resource.lower() not in str(entry.get("resource") or "").lower():
            continue
        if outcome and entry.get("outcome") != outcome:
            continue
        if not _within_window(entry, since, until):
            continue
        rows.append({field: entry.get(field) for field in _AUDIT_ITEM_FIELDS})
    # Newest first, matching the PG ORDER BY timestamp DESC.
    rows.sort(key=lambda row: str(row.get("timestamp") or ""), reverse=True)
    return rows


async def _soft_pool(request: Request) -> Any | None:
    """Return the asyncpg pool if present, else ``None`` (offline profile).

    Unlike ``api.dependencies.get_pool`` this never raises, so the audit read
    paths degrade to the ring buffer instead of returning 503.
    """
    return getattr(request.app.state, "pool", None)


async def search_audit_log(
    *,
    pool: asyncpg.Pool | None,
    user_id: str | None = None,
    role: str | None = None,
    action: str | None = None,
    resource: str | None = None,
    outcome: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
    workspace_id: str | None = None,
) -> dict:
    """Search the audit log. Returns {total, items, limit, offset}.

    Tenant-scoped via workspace_id: a caller with a workspace claim only
    sees their own audit entries. Calls without workspace_id see only
    rows where workspace_id IS NULL (legacy / global).

    Plain async callable so unit tests can invoke it directly without
    FastAPI dependency resolution. The HTTP route wrapper is below.

    ``pool is None`` selects the offline ring buffer (no PostgreSQL ⇒ no 500);
    otherwise the PostgreSQL ``audit_log`` table is queried as before.
    """

    if pool is None:
        filtered = _memory_filter(
            workspace_id=workspace_id,
            user_id=user_id,
            role=role,
            action=action,
            resource=resource,
            outcome=outcome,
            since=since,
            until=until,
        )
        return {
            "total": len(filtered),
            "items": filtered[offset : offset + limit],
            "limit": limit,
            "offset": offset,
        }

    clauses: list[str] = ["1=1"]
    params: list = []
    idx = 1

    def _add(clause: str, value) -> None:
        nonlocal idx
        clauses.append(clause.replace("$X", f"${idx}"))
        params.append(value)
        idx += 1

    # Workspace scoping always applies — it's the multi-tenant boundary
    if workspace_id:
        import uuid as _uuid

        _add("workspace_id = $X", _uuid.UUID(workspace_id))
    else:
        clauses.append("workspace_id IS NULL")

    if user_id:
        _add("user_id::text = $X", user_id)
    if role:
        _add("role = $X", role)
    if action:
        _add("action = $X", action.upper())
    if resource:
        _add("resource ILIKE $X", f"%{resource}%")
    if outcome:
        _add("outcome = $X", outcome)
    if since:
        _add("timestamp >= $X", since)
    if until:
        _add("timestamp < $X", until)

    where_sql = " AND ".join(clauses)

    count_sql = f"SELECT COUNT(*) AS n FROM audit_log WHERE {where_sql}"

    items_sql = f"""
        SELECT
            id,
            timestamp,
            user_id::text  AS user_id,
            role,
            action,
            resource,
            resource_id,
            outcome,
            request_id::text AS request_id,
            metadata
        FROM audit_log
        WHERE {where_sql}
        ORDER BY timestamp DESC
        LIMIT ${idx} OFFSET ${idx + 1}
    """

    try:
        async with pool.acquire() as conn:
            count_row = await conn.fetchrow(count_sql, *params)
            rows = await conn.fetch(items_sql, *params, limit, offset)
    except Exception as exc:
        logger.exception("Audit search failed: %s", exc)
        return {"total": 0, "items": [], "limit": limit, "offset": offset, "error": str(exc)}

    items = [
        {
            "id": r["id"],
            "timestamp": r["timestamp"].isoformat() if r["timestamp"] else None,
            "user_id": r["user_id"],
            "role": r["role"],
            "action": r["action"],
            "resource": r["resource"],
            "resource_id": r["resource_id"],
            "outcome": r["outcome"],
            "request_id": r["request_id"],
            "metadata": dict(r["metadata"]) if r["metadata"] else {},
        }
        for r in rows
    ]

    return {
        "total": int(count_row["n"]) if count_row else 0,
        "items": items,
        "limit": limit,
        "offset": offset,
    }


@router.get("/search")
async def search_audit_log_route(
    user_id: str | None = Query(None, description="Exact user_id match"),
    role: str | None = Query(None, description="Exact role match"),
    action: str | None = Query(None, description="HTTP method filter (GET, POST, ...)"),
    resource: str | None = Query(None, description="Substring match on URL path"),
    outcome: str | None = Query(None, description="allowed | denied | error"),
    since: datetime | None = Query(None, description="Start of time window (ISO-8601)"),
    until: datetime | None = Query(None, description="End of time window (ISO-8601)"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    pool: asyncpg.Pool | None = Depends(_soft_pool),
    workspace_id: str | None = Depends(get_workspace_id),
) -> dict:
    return await search_audit_log(
        pool=pool,
        user_id=user_id,
        role=role,
        action=action,
        resource=resource,
        outcome=outcome,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
        workspace_id=workspace_id,
    )


@router.get("/stats")
async def audit_stats(
    days: int = Query(7, ge=1, le=365),
    pool: asyncpg.Pool | None = Depends(_soft_pool),
) -> dict:
    """High-level audit aggregates for the dashboard overview card."""
    if pool is None:
        # Offline profile — aggregate the ring buffer instead of failing.
        rows = _memory_filter()
        return {
            "window_days": days,
            "total": len(rows),
            "denied": sum(1 for r in rows if r.get("outcome") == "denied"),
            "errors": sum(1 for r in rows if r.get("outcome") == "error"),
            "distinct_users": len({r.get("user_id") for r in rows}),
            "top_resources": [],
        }
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*)                                        AS total,
                    COUNT(*) FILTER (WHERE outcome = 'denied')      AS denied,
                    COUNT(*) FILTER (WHERE outcome = 'error')       AS errors,
                    COUNT(DISTINCT user_id)                         AS distinct_users
                FROM audit_log
                WHERE timestamp > now() - make_interval(days => $1)
                """,
                days,
            )
            top_resources = await conn.fetch(
                """
                SELECT resource, COUNT(*) AS hits
                FROM audit_log
                WHERE timestamp > now() - make_interval(days => $1)
                GROUP BY resource
                ORDER BY hits DESC
                LIMIT 10
                """,
                days,
            )
    except Exception as exc:
        logger.exception("Audit stats failed: %s", exc)
        return {"error": str(exc)}

    return {
        "window_days": days,
        "total": int(row["total"]) if row else 0,
        "denied": int(row["denied"]) if row else 0,
        "errors": int(row["errors"]) if row else 0,
        "distinct_users": int(row["distinct_users"]) if row else 0,
        "top_resources": [{"resource": r["resource"], "hits": r["hits"]} for r in top_resources],
    }


# --------------------------------------------------------------------------- #
# /audit/export                                                                #
# --------------------------------------------------------------------------- #

_SUPPORTED_FORMATS = ("csv", "json")
_EXPORT_LIMIT = 10_000


def _rows_to_csv(rows: list[dict[str, Any]]) -> str:
    """Render audit items as CSV (header row always present)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(_AUDIT_ITEM_FIELDS)
    for row in rows:
        writer.writerow(
            [
                json.dumps(row.get(field), ensure_ascii=False)
                if field == "metadata"
                else ("" if row.get(field) is None else row.get(field))
                for field in _AUDIT_ITEM_FIELDS
            ]
        )
    return buffer.getvalue()


async def export_audit_log(
    *,
    pool: asyncpg.Pool | None,
    workspace_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    fmt: str = "json",
) -> Response:
    """Export the audit log as CSV or JSON, tenant + time-scoped.

    ``pool is None`` (offline profile) exports the in-memory ring buffer, which
    is the same source ``search_audit_log`` reads in that profile — so an export
    never 500s just because there is no PostgreSQL.
    """
    normalised = (fmt or "").strip().lower()
    if normalised not in _SUPPORTED_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported format '{fmt}'; expected one of {', '.join(_SUPPORTED_FORMATS)}",
        )

    result = await search_audit_log(
        pool=pool,
        workspace_id=workspace_id,
        since=since,
        until=until,
        limit=_EXPORT_LIMIT,
        offset=0,
    )
    rows: list[dict[str, Any]] = result.get("items", [])

    if normalised == "csv":
        body = _rows_to_csv(rows)
        filename = f"audit-export-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.csv"
        return Response(
            content=body,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return JSONResponse(
        content={"total": len(rows), "items": rows},
        media_type="application/json",
    )


@router.get("/export")
async def export_audit_log_route(
    format: str = Query("json", description="Export format: csv | json"),
    from_: datetime | None = Query(None, alias="from", description="Start of window (ISO-8601)"),
    to: datetime | None = Query(None, description="End of window (ISO-8601)"),
    pool: asyncpg.Pool | None = Depends(_soft_pool),
    workspace_id: str | None = Depends(get_workspace_id),
) -> Response:
    """Download the audit log for the current tenant as CSV or JSON."""
    return await export_audit_log(
        pool=pool,
        workspace_id=workspace_id,
        since=from_,
        until=to,
        fmt=format,
    )
