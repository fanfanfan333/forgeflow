"""Workspace membership lookup — single source of truth for tenant scoping.

A user's accessible workspaces come from the workspace_members table (seeded
by tenant-admin flow). The `/auth/login` route consults this before honoring
any workspace_id in the request body — closing the C-4 (workspace
self-assertion) vulnerability from SECURITY_AUDIT.md.

The helper falls back to a permissive "no scope" answer when the workspace
table does not yet exist (early dev environments before migration 005 has
run). Once 005 is applied the strict path takes over automatically.
"""

from __future__ import annotations

import logging
import uuid

import asyncpg

logger = logging.getLogger(__name__)


async def user_workspaces(pool: asyncpg.Pool, user_id: str) -> set[str]:
    """Return the set of workspace UUIDs this user belongs to.

    Returns an empty set when the user is unknown, when the user_id is not a
    UUID, or when the workspace_members table is missing (pre-migration). The
    caller decides whether an empty set is "no workspace claim allowed" or
    "single-tenant fallback".
    """
    try:
        uuid.UUID(user_id)
    except (ValueError, TypeError):
        return set()

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT workspace_id::text AS ws FROM workspace_members WHERE user_id = $1",
                uuid.UUID(user_id),
            )
            return {r["ws"] for r in rows}
    except asyncpg.UndefinedTableError:
        # Migration 005 not applied yet — single-tenant deployment.
        return set()
    except Exception as exc:
        logger.warning("workspace_members lookup failed: %s", exc)
        return set()


async def user_is_member(pool: asyncpg.Pool, user_id: str, workspace_id: str) -> bool:
    """Return True if (user_id, workspace_id) ∈ workspace_members.

    Returns False on any UUID parse failure or DB error — fail closed.
    """
    try:
        uid = uuid.UUID(user_id)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM tenant_memberships WHERE user_id=$1 AND tenant_id=$2",
                uid,
                str(workspace_id),
            )
            if row is not None:
                return True
            try:
                ws = uuid.UUID(workspace_id)
            except (ValueError, TypeError):
                return False
            return await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM workspace_members WHERE user_id=$1 AND workspace_id=$2)",
                uid,
                ws,
            )
    except (ValueError, TypeError, asyncpg.PostgresError):
        return False
