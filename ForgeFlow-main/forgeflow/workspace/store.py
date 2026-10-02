"""``WorkspaceStore`` — the persisted session / parent-run facts (INC32 ADR-02).

Two implementations share one tenant-first signature (house rule: ``tenant_id``
is the first positional argument of every read so row-level isolation can never
be forgotten — see ``forgeflow.repositories.base``):

  * :class:`MemoryWorkspaceStore` — in-process dicts, stdlib only (the offline
    profile; a restart drops it exactly like it drops ``MemoryRunStore``).
  * :class:`PgWorkspaceStore` — asyncpg-backed, talks to the ``workspace_runs``
    table created by migration ``016``.

Both implement :class:`WorkspaceStore`. ``save`` is an upsert keyed by
``run_id`` so the pre-registered ``running`` header is overwritten by the
terminal header for the same run without creating a second row.

Importing this module opens no connection.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from forgeflow.config import get_settings
from forgeflow.repositories.base import TenantScopedRepository
from forgeflow.workspace.models import WorkspaceRunRecord

logger = logging.getLogger(__name__)

__all__ = [
    "WorkspaceStore",
    "MemoryWorkspaceStore",
    "PgWorkspaceStore",
    "get_workspace_store",
    "reset_workspace_store",
    "clear_workspace_store",
    "INTERRUPTED_STATUS",
    "TERMINAL_STATUSES",
]

#: Run statuses that mean the run is already done (a restart reconciliation must
#: never rewrite one of these into ``interrupted``).
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "aborted", "interrupted", "rejected"}
)
#: The status a still-``running`` row gets when the process restarts (honest:
#: the in-process task that owned it is gone; the run did not complete).
INTERRUPTED_STATUS = "interrupted"


def _now_iso() -> str:
    """Timezone-aware ISO-8601 UTC timestamp — the one clock for soft deletes."""
    return datetime.now(timezone.utc).isoformat()


@runtime_checkable
class WorkspaceStore(Protocol):
    """Persistence for the INC32 run header + relationship + artifacts facts."""

    async def save(self, record: WorkspaceRunRecord) -> WorkspaceRunRecord:
        """Insert or replace a run header (by ``run_id``). Returns it back."""
        ...

    async def get(self, tenant_id: str | None, run_id: str) -> WorkspaceRunRecord | None:
        """Fetch one run header, tenant-scoped. ``None`` when not found."""
        ...

    async def list_sessions(self, tenant_id: str | None, limit: int = 20) -> list[dict[str, Any]]:
        """Group the tenant's runs into sessions (newest activity first)."""
        ...

    async def list_session_runs(
        self, tenant_id: str | None, session_id: str
    ) -> list[WorkspaceRunRecord]:
        """The runs of one session, oldest first (the conversation order)."""
        ...

    async def list_recent_for_tenant(
        self, tenant_id: str | None, limit: int = 200
    ) -> list[WorkspaceRunRecord]:
        """The tenant's persisted run headers, newest activity first.

        INC40 — this is the **read-side, tenant-scoped** hydration source
        (INC33 / ADR-02「跨重启可查」). It replaces the old global ``list_recent``
        whose ``ORDER BY created_at DESC LIMIT 200`` (no tenant filter) let other
        tenants' newer rows push a tenant's own history out of the window — a
        non-deterministic defect that went red purely as the shared DB grew.
        """
        ...

    async def mark_interrupted(self, tenant_id: str | None, run_id: str) -> None:
        """Mark a still-``running`` run ``interrupted`` (no-op otherwise)."""
        ...

    async def interrupt_stale_running(self) -> int:
        """Mark every still-``running`` run ``interrupted``; return the count.

        Used once at startup: the in-process task references are necessarily
        gone after a restart, so a row left ``running`` is honestly reported as
        ``interrupted`` rather than pretending it finished.
        """
        ...

    async def soft_delete_session(
        self, tenant_id: str | None, session_id: str
    ) -> int:
        """Soft-delete a session's runs; return how many rows were marked.

        INC42 / Q4=A + Q5=B — deleting history means **soft delete**: every run
        of the session gets ``deleted_at = now`` (the rows are **kept**, so the
        audit chain survives). Every read path then filters ``deleted_at IS
        NULL``. ``tenant_id`` is a mandatory predicate so a cross-tenant call
        can never touch another tenant's session. Returns the number of rows
        actually marked (``0`` ⇒ the session is unknown / already deleted, which
        the route turns into a 404).
        """
        ...


def _session_groups(records: list[WorkspaceRunRecord], limit: int) -> list[dict[str, Any]]:
    """Group run headers into session summaries (shared by both backends).

    A session's ``title`` is its **earliest** run's title (the session began
    there); ``created_at`` is that earliest run's timestamp; ``run_count`` is how
    many runs the session holds; ``latest_status`` is the newest run's status.
    Sessions are ordered by their **latest** activity (newest first) so the
    conversation list reflects recency.
    """
    by_session: dict[str, list[WorkspaceRunRecord]] = {}
    for rec in records:
        sid = rec.session_id or rec.run_id
        by_session.setdefault(sid, []).append(rec)
    groups: list[dict[str, Any]] = []
    for sid, runs in by_session.items():
        ordered = sorted(runs, key=lambda r: r.created_at or "")
        earliest = ordered[0]
        latest = ordered[-1]
        groups.append(
            {
                "session_id": sid,
                "title": earliest.title or earliest.intent[:60],
                "created_at": earliest.created_at,
                "run_count": len(ordered),
                "latest_status": latest.status,
                "_latest_at": latest.created_at,
            }
        )
    groups.sort(key=lambda g: g["_latest_at"] or "", reverse=True)
    for group in groups:
        group.pop("_latest_at", None)
    return groups[: max(limit, 0)]


# --------------------------------------------------------------------------- #
# Memory (offline profile)                                                     #
# --------------------------------------------------------------------------- #
#: Module-level singleton so every factory call shares one store (mirrors
#: ``repositories/memory/resource_repo.py``).
_STORE: dict[str, dict[str, WorkspaceRunRecord]] = {}
_LOCK = asyncio.Lock()


def clear_workspace_store() -> None:
    """Reset all in-memory workspace state. Test helper only."""
    _STORE.clear()


def _clone(record: WorkspaceRunRecord) -> WorkspaceRunRecord:
    """Deep copy so the store never aliases a caller-owned instance."""
    return copy.deepcopy(record)


class MemoryWorkspaceStore(TenantScopedRepository):
    """Dict-backed ``WorkspaceStore`` (identical surface to the PG impl)."""

    async def save(self, record: WorkspaceRunRecord) -> WorkspaceRunRecord:
        key = self.scope_key(record.tenant_id)
        async with _LOCK:
            _STORE.setdefault(key, {})[record.run_id] = _clone(record)
        return record

    async def get(self, tenant_id: str | None, run_id: str) -> WorkspaceRunRecord | None:
        stored = _STORE.get(self.scope_key(tenant_id), {}).get(run_id)
        # INC42 — a soft-deleted row is invisible to every read path.
        if stored is None or stored.deleted_at is not None:
            return None
        return _clone(stored)

    async def list_sessions(
        self, tenant_id: str | None, limit: int = 20
    ) -> list[dict[str, Any]]:
        rows = [
            r
            for r in _STORE.get(self.scope_key(tenant_id), {}).values()
            if r.deleted_at is None
        ]
        return _session_groups([_clone(r) for r in rows], limit)

    async def list_session_runs(
        self, tenant_id: str | None, session_id: str
    ) -> list[WorkspaceRunRecord]:
        rows = [
            r
            for r in _STORE.get(self.scope_key(tenant_id), {}).values()
            if r.session_id == session_id and r.deleted_at is None
        ]
        rows.sort(key=lambda r: r.created_at or "")
        return [_clone(r) for r in rows]

    async def list_recent_for_tenant(
        self, tenant_id: str | None, limit: int = 200
    ) -> list[WorkspaceRunRecord]:
        rows = [
            _clone(r)
            for r in _STORE.get(self.scope_key(tenant_id), {}).values()
            if r.deleted_at is None
        ]
        rows.sort(key=lambda r: r.created_at or "", reverse=True)
        return rows[: max(limit, 0)]

    async def mark_interrupted(self, tenant_id: str | None, run_id: str) -> None:
        key = self.scope_key(tenant_id)
        async with _LOCK:
            record = _STORE.get(key, {}).get(run_id)
            if record is not None and record.status not in TERMINAL_STATUSES:
                record.status = INTERRUPTED_STATUS
                record.updated_at = WorkspaceRunRecord(
                    run_id=record.run_id, tenant_id=record.tenant_id, session_id=record.session_id
                ).updated_at

    async def interrupt_stale_running(self) -> int:
        count = 0
        async with _LOCK:
            for bucket in _STORE.values():
                for record in bucket.values():
                    if record.status not in TERMINAL_STATUSES:
                        record.status = INTERRUPTED_STATUS
                        count += 1
        return count

    async def soft_delete_session(self, tenant_id: str | None, session_id: str) -> int:
        """Mark every live run of ``session_id`` deleted (INC42 / Q5=B)."""
        now = _now_iso()
        marked = 0
        async with _LOCK:
            for record in _STORE.get(self.scope_key(tenant_id), {}).values():
                if record.session_id == session_id and record.deleted_at is None:
                    record.deleted_at = now
                    record.updated_at = now
                    marked += 1
        return marked


# --------------------------------------------------------------------------- #
# PostgreSQL (asyncpg, lazy pool)                                              #
# --------------------------------------------------------------------------- #
def _as_dict(value: Any) -> dict[str, Any]:
    """Decode a JSONB column that may arrive as ``dict`` or JSON text."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _as_list(value: Any) -> list[dict[str, Any]]:
    """Decode a JSONB array column (``dict`` items only; malformed dropped)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, dict)]
    return []


class PgWorkspaceStore(TenantScopedRepository):
    """asyncpg-backed ``WorkspaceStore`` (talks to table ``workspace_runs``)."""

    def __init__(self, default_tenant: str = "default", pool: Any | None = None) -> None:
        super().__init__(default_tenant)
        self._pool = pool

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        from forgeflow.database import get_pool

        return await get_pool()

    @staticmethod
    def _to_record(row: Any) -> WorkspaceRunRecord:
        d = dict(row)
        return WorkspaceRunRecord(
            run_id=str(d.get("run_id") or ""),
            tenant_id=(str(d["tenant_id"]) if d.get("tenant_id") else None),
            session_id=str(d.get("session_id") or ""),
            parent_run_id=str(d.get("parent_run_id") or ""),
            actor_user_id=str(d.get("actor_user_id") or ""),
            actor_role=str(d.get("actor_role") or ""),
            intent=str(d.get("intent") or ""),
            title=str(d.get("title") or ""),
            workflow_type=str(d.get("workflow_type") or "generic"),
            status=str(d.get("status") or "running"),
            outcome=str(d.get("outcome") or ""),
            declared_inputs=_as_dict(d.get("declared_inputs")),
            artifacts=_as_list(d.get("artifacts")),
            # INC42 — the persisted executor provenance + soft-delete marker.
            runtime_mode=str(d.get("runtime_mode") or ""),
            llm=_as_dict(d.get("llm")),
            deleted_at=(d.get("deleted_at") or None),
            created_at=str(d.get("created_at") or ""),
            completed_at=(d.get("completed_at") or None),
            updated_at=str(d.get("updated_at") or ""),
        )

    async def save(self, record: WorkspaceRunRecord) -> WorkspaceRunRecord:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO workspace_runs
                  (run_id, tenant_id, session_id, parent_run_id, actor_user_id,
                   actor_role, intent, title, workflow_type, status, outcome,
                   declared_inputs, artifacts, runtime_mode, llm,
                   created_at, completed_at, updated_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,$13::jsonb,$14,$15::jsonb,$16,$17,$18)
                ON CONFLICT (run_id) DO UPDATE SET
                  tenant_id=EXCLUDED.tenant_id, session_id=EXCLUDED.session_id,
                  parent_run_id=EXCLUDED.parent_run_id,
                  actor_user_id=EXCLUDED.actor_user_id, actor_role=EXCLUDED.actor_role,
                  intent=EXCLUDED.intent, title=EXCLUDED.title,
                  workflow_type=EXCLUDED.workflow_type, status=EXCLUDED.status,
                  outcome=EXCLUDED.outcome, declared_inputs=EXCLUDED.declared_inputs,
                  artifacts=EXCLUDED.artifacts, runtime_mode=EXCLUDED.runtime_mode,
                  llm=EXCLUDED.llm, completed_at=EXCLUDED.completed_at,
                  updated_at=EXCLUDED.updated_at
                """,
                record.run_id,
                self.scope_key(record.tenant_id),
                record.session_id,
                record.parent_run_id or None,
                record.actor_user_id,
                record.actor_role,
                record.intent,
                record.title,
                record.workflow_type,
                record.status,
                record.outcome,
                # NOTE (unchanged, out of INC42 scope): ``declared_inputs`` /
                # ``artifacts`` are ``json.dumps``-ed into a ``jsonb`` column, so
                # the DB stores a **double-encoded string**; the read side
                # tolerates it via ``_as_dict`` / ``_as_list``. ``llm`` follows
                # the same convention on purpose (consistency).
                json.dumps(record.declared_inputs, ensure_ascii=False),
                json.dumps(record.artifacts, ensure_ascii=False),
                record.runtime_mode,
                json.dumps(record.llm, ensure_ascii=False),
                record.created_at,
                record.completed_at,
                record.updated_at,
            )
        return record

    async def get(self, tenant_id: str | None, run_id: str) -> WorkspaceRunRecord | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM workspace_runs "
                "WHERE run_id=$1 AND tenant_id IS NOT DISTINCT FROM $2 "
                "AND deleted_at IS NULL",
                run_id,
                self.scope_key(tenant_id),
            )
        return self._to_record(row) if row else None

    async def list_sessions(
        self, tenant_id: str | None, limit: int = 20
    ) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM workspace_runs "
                "WHERE tenant_id IS NOT DISTINCT FROM $1 AND deleted_at IS NULL "
                "ORDER BY created_at DESC",
                self.scope_key(tenant_id),
            )
        return _session_groups([self._to_record(r) for r in rows], limit)

    async def list_session_runs(
        self, tenant_id: str | None, session_id: str
    ) -> list[WorkspaceRunRecord]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM workspace_runs "
                "WHERE tenant_id IS NOT DISTINCT FROM $1 AND session_id=$2 "
                "AND deleted_at IS NULL "
                "ORDER BY created_at ASC",
                self.scope_key(tenant_id),
                session_id,
            )
        return [self._to_record(r) for r in rows]

    async def list_recent_for_tenant(
        self, tenant_id: str | None, limit: int = 200
    ) -> list[WorkspaceRunRecord]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM workspace_runs "
                "WHERE tenant_id IS NOT DISTINCT FROM $1 AND deleted_at IS NULL "
                "ORDER BY created_at DESC LIMIT $2",
                self.scope_key(tenant_id),
                max(limit, 0),
            )
        return [self._to_record(r) for r in rows]

    async def mark_interrupted(self, tenant_id: str | None, run_id: str) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE workspace_runs SET status=$3, updated_at=$4 "
                "WHERE run_id=$1 AND tenant_id IS NOT DISTINCT FROM $2 "
                "AND status NOT IN ('completed','failed','aborted','interrupted','rejected')",
                run_id,
                self.scope_key(tenant_id),
                INTERRUPTED_STATUS,
                WorkspaceRunRecord(
                    run_id=run_id, tenant_id=tenant_id, session_id=""
                ).updated_at,
            )

    async def interrupt_stale_running(self) -> int:
        pool = await self._get_pool()
        # PostgreSQL forbids aggregates in ``RETURNING`` (``count(*)`` raises
        # ``GroupingError``), and the broad ``except`` in
        # ``RunDispatcher.reconcile_on_start`` swallowed that into a warning —
        # so the restart reconciliation silently never ran on this backend.
        # Return the affected rows and count them client-side instead.
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "UPDATE workspace_runs SET status=$1, updated_at=$2 "
                "WHERE status NOT IN ('completed','failed','aborted','interrupted','rejected') "
                "RETURNING run_id",
                INTERRUPTED_STATUS,
                WorkspaceRunRecord(
                    run_id="_", tenant_id=None, session_id=""
                ).updated_at,
            )
        return len(rows)

    async def soft_delete_session(self, tenant_id: str | None, session_id: str) -> int:
        """Soft-delete every live run of ``session_id`` (INC42 / Q5=B).

        The ``tenant_id`` predicate is **mandatory** (house rule: a cross-tenant
        call must never touch another tenant's session — the INC40 P0 lesson).
        ``RETURNING run_id`` so the count is computed client-side (PostgreSQL
        forbids ``count(*)`` in ``RETURNING``).
        """
        pool = await self._get_pool()
        now = _now_iso()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "UPDATE workspace_runs SET deleted_at=$3, updated_at=$3 "
                "WHERE tenant_id IS NOT DISTINCT FROM $1 AND session_id=$2 "
                "AND deleted_at IS NULL RETURNING run_id",
                self.scope_key(tenant_id),
                session_id,
                now,
            )
        return len(rows)


# --------------------------------------------------------------------------- #
# Factory                                                                      #
# --------------------------------------------------------------------------- #
_CACHE: dict[str, WorkspaceStore] = {}


def get_workspace_store() -> WorkspaceStore:
    """Return the ``WorkspaceStore`` for the configured storage backend.

    Cached per backend so the in-memory dict is shared across callers (a fresh
    instance per call would lose every write). Defaults to the memory backend
    for anything that is not explicitly ``postgres``.
    """
    backend = str(get_settings().storage_backend or "memory").lower()
    store = _CACHE.get(backend)
    if store is None:
        default_tenant = get_settings().default_tenant_id
        if backend == "postgres":
            store = PgWorkspaceStore(default_tenant=default_tenant)
        else:
            store = MemoryWorkspaceStore(default_tenant=default_tenant)
        _CACHE[backend] = store
    return store


def reset_workspace_store() -> None:
    """Drop the cached store(s) and any in-memory state. Test helper only."""
    _CACHE.clear()
    clear_workspace_store()
