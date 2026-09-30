"""Memory routes — store and search semantic memory vectors.

Hardening (SECURITY_AUDIT.md §6, §7):
  - Namespace is forced to start with `workspace/{workspace_id}/` (or
    `global/` for unscoped callers). Cross-tenant recall is impossible
    without an admin override.
  - Free-form metadata is preserved but provenance is added automatically
    (`written_by_user_id`, `workspace_id`) so recall can filter.
  - Recall returns only namespaces the caller is allowed to see.
"""

from __future__ import annotations

import logging

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from forgeflow.api.dependencies import get_current_user, get_pool, get_workspace_id
from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.hub_schemas import (
    MemoryCreateRequest,
    MemoryLifecycleResponse,
    MemoryListResponse,
    MemoryResponse,
    MemoryScopeResponse,
    MemorySweepResponse,
)
from forgeflow.api.schemas import MemorySearchResult, MemoryStoreRequest, MemoryStoreResponse
from forgeflow.config import get_settings
from forgeflow.experience.memory_store import lifecycle_sweep as run_lifecycle_sweep
from forgeflow.experience.memory_store import lifecycle_summary as get_lifecycle_summary
from forgeflow.experience.memory_store import list_memories as list_scoped_memories
from forgeflow.experience.memory_store import save_memory as save_scoped_memory
from forgeflow.experience.memory_store import search as search_scoped_memories
from forgeflow.experience.memory_types import default_type_for_scope
from forgeflow.experience.promotion import PromotionError, promote_memory
from forgeflow.experience.scopes import list_scopes
from forgeflow.memory.memory_manager import MemoryManager
from forgeflow.rbac.models import UserContext

logger = logging.getLogger(__name__)
router = APIRouter()


async def _optional_pool(request: Request) -> asyncpg.Pool | None:
    """Return the app pool when initialised, else ``None``.

    Unlike ``get_pool`` this never raises, so the search route can serve the
    PostgreSQL-free ``memory`` backend (INC2 D1) instead of returning 503.
    """
    return getattr(request.app.state, "pool", None)


def _namespace_prefix(workspace_id: str | None) -> str:
    return f"workspace/{workspace_id}/" if workspace_id else "global/"


def _require_owned_namespace(namespace: str, workspace_id: str | None) -> None:
    """Reject any namespace that doesn't start with the caller's prefix."""
    prefix = _namespace_prefix(workspace_id)
    if not namespace.startswith(prefix):
        raise HTTPException(
            status_code=403,
            detail=(
                f"namespace must start with '{prefix}' — "
                "cross-tenant memory access is blocked"
            ),
        )


def _memory_response(entry) -> MemoryResponse:
    """Map a ``MemoryEntry`` to the wire schema (INC9 B2/B3 fields included)."""
    memory_type = getattr(entry, "memory_type", "") or default_type_for_scope(entry.scope)
    return MemoryResponse(
        id=entry.id,
        tenant_id=entry.tenant_id,
        scope=entry.scope,
        content=entry.content,
        team_id=entry.team_id,
        namespace=entry.namespace,
        metadata=entry.metadata,
        created_at=entry.created_at,
        memory_type=memory_type,
        reuse_count=int(getattr(entry, "reuse_count", 0) or 0),
        archived=bool(getattr(entry, "archived", False)),
    )


@router.post("/store", response_model=MemoryStoreResponse)
async def store_memory(
    request: MemoryStoreRequest,
    pool: asyncpg.Pool = Depends(get_pool),
    user: UserContext = Depends(get_current_user),
    workspace_id: str | None = Depends(get_workspace_id),
):
    """Embed and store a memory in the vector store (tenant-scoped)."""
    _require_owned_namespace(request.namespace, workspace_id)

    metadata = dict(request.metadata or {})
    metadata.setdefault("written_by_user_id", user.user_id)
    if workspace_id:
        metadata.setdefault("workspace_id", workspace_id)
    metadata.setdefault("source_trust_level", "user")

    manager = MemoryManager(pool)
    try:
        memory_id = await manager.remember(
            content=request.content,
            namespace=request.namespace,
            metadata=metadata,
            ttl_hours=request.ttl_hours,
        )
    except Exception as e:
        logger.exception("memory store failed")
        raise HTTPException(status_code=500, detail=f"Failed to store memory: {e}") from e

    return MemoryStoreResponse(memory_id=memory_id)


@router.get("/search", response_model=list[MemorySearchResult])
async def search_memory(
    q: str = Query(..., description="Search query", max_length=2000),
    k: int = Query(5, ge=1, le=20),
    namespace: str | None = Query(None, description="Restrict to namespace"),
    scope: str | None = Query(None, description="Restrict to a memory layer (memory backend)"),
    pool: asyncpg.Pool | None = Depends(_optional_pool),
    workspace_id: str | None = Depends(get_workspace_id),
    tenant: str = Depends(resolve_tenant),
):
    """Semantic search over stored memories — tenant-scoped.

    INC2 D1 (§2.17): both backends return the exact same
    ``MemorySearchResult`` shape. The ``memory`` backend now serves results from
    the scope-partitioned ``memory_store`` using the dependency-free
    ``experience.embedding.embed_text`` cosine similarity — so it returns 200
    with no PostgreSQL pool and no external embedding key, closing the old
    MemoryManager/pgvector fork. The ``postgres`` backend keeps its prior
    pgvector recall unchanged (regression-protected).

    RBAC: ``GET /memory/search`` is already covered — the ``("GET", "/memory")``
    entry in ``ROUTE_PERMISSION_MAP`` (``rbac/policies.py``) matches it via
    ``RBACMiddleware._resolve_permission``'s prefix match, so no per-path entry
    is needed.
    """
    backend = get_settings().storage_backend.lower()

    if backend != "postgres":
        pairs = await search_scoped_memories(tenant, q, k, scope=scope)
        return [
            MemorySearchResult(
                id=entry.id,
                content=entry.content,
                similarity=round(float(sim), 6),
                namespace=entry.namespace,
                metadata=entry.metadata,
            )
            for entry, sim in pairs
        ]

    # --- postgres backend (behaviour unchanged) ---------------------------
    if pool is None:
        raise HTTPException(status_code=503, detail="Database pool not initialised")

    prefix = _namespace_prefix(workspace_id)

    if namespace is not None:
        _require_owned_namespace(namespace, workspace_id)
        search_ns = namespace
    else:
        # Force prefix even when caller omits the param.
        search_ns = prefix.rstrip("/")

    manager = MemoryManager(pool)
    try:
        results = await manager.recall(query=q, k=k, namespace=search_ns)
    except Exception as e:
        logger.exception("memory search failed")
        raise HTTPException(status_code=500, detail=f"Memory search failed: {e}") from e

    # Defensive: drop any row whose namespace escaped the prefix (covers
    # legacy unscoped memories that should not appear in tenant queries).
    safe = [r for r in results if str(r.get("namespace", "")).startswith(prefix)]

    return [
        MemorySearchResult(
            id=r["id"],
            content=r["content"],
            similarity=r["similarity"],
            namespace=r["namespace"],
            metadata=r["metadata"],
        )
        for r in safe
    ]


@router.delete("/{memory_id}")
async def delete_memory(    memory_id: str,
    pool: asyncpg.Pool = Depends(get_pool),
    workspace_id: str | None = Depends(get_workspace_id),
):
    """Delete a specific memory by ID (only if it lives in caller's namespace)."""
    prefix = _namespace_prefix(workspace_id)

    # Fetch first to check ownership — we can't trust the id alone.
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT namespace FROM memory_vectors WHERE id::text = $1",
            memory_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    if not str(row["namespace"]).startswith(prefix):
        # Treat as not-found — don't reveal existence of another tenant's row.
        raise HTTPException(status_code=404, detail="Memory not found")

    manager = MemoryManager(pool)
    deleted = await manager.forget(memory_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"deleted": True, "memory_id": memory_id}


# --------------------------------------------------------------------------- #
# AgentFlow Memory Hub — five-layer scope view (docs §4.1 / P0-13)             #
# Backed by forgeflow.experience.memory_store (in-process, scope-partitioned)  #
# so it works with no PostgreSQL. The legacy /store + /search above keep using #
# pgvector unchanged.                                                          #
# --------------------------------------------------------------------------- #

@router.get("/scopes", response_model=list[MemoryScopeResponse])
async def memory_scopes():
    """Return the five memory layers with their write/read rules."""
    return [MemoryScopeResponse(**scope) for scope in list_scopes()]


@router.get("", response_model=MemoryListResponse)
async def list_memory_entries(
    scope: str | None = Query(None, description="Filter by memory layer"),
    team_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    tenant: str = Depends(resolve_tenant),
):
    """List hub memories (newest first), optionally filtered by scope/team."""
    rows = await list_scoped_memories(
        tenant, scope=scope, team_id=team_id, limit=limit, offset=offset
    )
    return MemoryListResponse(
        total=len(rows),
        items=[_memory_response(r) for r in rows],
    )


@router.post("", response_model=MemoryResponse)
async def create_memory_entry(
    request: MemoryCreateRequest,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """Write a scoped memory. Runs the DLP scan before persisting."""
    from forgeflow.governance.dlp import DlpGate

    scan = DlpGate().scan(request.content)
    entry = await save_scoped_memory(
        tenant,
        request.scope,
        scan.redacted or request.content,
        team_id=request.team_id,
        metadata={**request.metadata, "pii_found": scan.pii_found},
        actor_id=user.user_id,
    )
    return _memory_response(entry)


# --------------------------------------------------------------------------- #
# Memory lifecycle — Score / Decay / Archive (INC9 B2, docs/sop/12-INC9 §2.2)  #
# ⚠ ROUTE ORDER: these MUST be declared BEFORE POST /{memory_id}/promote below. #
# ``/lifecycle/sweep`` and ``/{memory_id}/promote`` have the same segment count,#
# so FastAPI would capture ``lifecycle`` as ``{memory_id}`` if the promote route #
# came first. Declaring them here (longest/specific first) avoids that.         #
# --------------------------------------------------------------------------- #

@router.get("/lifecycle", response_model=MemoryLifecycleResponse)
async def memory_lifecycle(
    tenant: str = Depends(resolve_tenant),
) -> MemoryLifecycleResponse:
    """Read-only lifecycle health (active/archived counts + mean score).

    RBAC: covered by the ``("GET", "/memory")`` entry via longest-prefix match.
    """
    summary = await get_lifecycle_summary(tenant)
    return MemoryLifecycleResponse(**summary)


@router.post("/lifecycle/sweep", response_model=MemorySweepResponse)
async def memory_lifecycle_sweep(
    tenant: str = Depends(resolve_tenant),
) -> MemorySweepResponse:
    """Run one score/decay/archive pass (idempotent; marker-only archiving).

    No-op unless ``Settings.memory_decay_enabled`` is true (default). RBAC:
    covered by the ``("POST", "/memory")`` entry via longest-prefix match.
    """
    result = await run_lifecycle_sweep(tenant)
    return MemorySweepResponse(**result)


# --------------------------------------------------------------------------- #
# Org-Memory promotion (INC2 A6, §2.6)                                         #
# --------------------------------------------------------------------------- #

class MemoryPromoteRequest(BaseModel):
    """Body for ``POST /memory/{memory_id}/promote``."""

    to_scope: str = Field(..., description="Target scope: team | org")
    from_scope: str | None = Field(
        None, description="Expected current scope (optional integrity check)"
    )
    reason: str = ""
    team_id: str | None = None


@router.post("/{memory_id}/promote", response_model=MemoryResponse)
async def promote_memory_entry(
    memory_id: str,
    request: MemoryPromoteRequest,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """Promote a scoped memory up the hierarchy (user → team → org).

    The source scope must be promotable (EPISODIC/ORG are rejected) and
    promoting to the org layer requires the admin role. Every promotion writes
    a ``memory.promote`` audit event.

    RBAC: ``POST /memory/{memory_id}/promote`` is already covered — the
    ``("POST", "/memory")`` entry in ``ROUTE_PERMISSION_MAP``
    (``rbac/policies.py``) matches it via ``RBACMiddleware._resolve_permission``'s
    longest-prefix match (⇒ ``write:memory``), so no per-path entry is needed.
    The stricter "org target is admin-only" rule is enforced in the handler
    (``memory.promote_memory``), which the path prefix cannot express.
    """
    try:
        entry = await promote_memory(
            tenant,
            memory_id,
            request.from_scope,
            request.to_scope,
            actor_id=user.user_id,
            role=user.role,
            reason=request.reason,
            team_id=request.team_id,
        )
    except PromotionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    return _memory_response(entry)
