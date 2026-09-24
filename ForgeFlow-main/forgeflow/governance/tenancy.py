"""Tenant isolation helpers (docs §6.1 / P0-10).

``TenantIsolation`` centralises the row-level rules so every hub enforces them
the same way: reads that miss the tenant return empty (→ 404 upstream, never
revealing existence), writes that cross tenants raise ``PermissionError``
(→ 403).

Tenant-isolation strategy — adjudicated (INC9 B3, review finding ⑨)
------------------------------------------------------------------
The platform's isolation strategy is **application-layer row filtering**, and
that is a deliberate choice, not a default-by-accident
(docs/sop/12-INC9-DESIGN.md §2.3):

* the whole repo follows one house rule — ``tenant_id`` is the first argument of
  every repository read/write and every query filters by it
  (``repositories/base.py``);
* it covers the **in-process** stores (``experience.memory_store``, the hub run
  store) that are the bulk of hub data — **Postgres RLS cannot see those
  objects**, so choosing RLS would give a false sense of coverage;
* it is identical on the offline (``memory``) profile and PostgreSQL, so there
  is a single code path rather than two security implementations that can drift.

Rejected alternatives and why: **Postgres RLS** (needs a per-connection
``SET app.tenant_id`` GUC, fragile against the shared asyncpg pool, and cannot be
reproduced on the offline profile → "backend-only" assumptions); **schema-per-
tenant** (each tenant's schema fans out the migration → breaks the single-head
alembic constraint); **physical isolation** (one DB per tenant → violates the
"no extra database" rule and multiplies ops cost). Cross-tenant skill/hub sharing
is additionally gated by the existing DLP + trust-baseline scan
(``skills/trust_baseline.verify_trust_baseline``) and ``shared`` defaulting to
FALSE — no new scanner is introduced here.
"""

from __future__ import annotations

from typing import Any, Iterable

from forgeflow.config import get_settings
from forgeflow.governance.context import get_current_tenant


class TenantIsolation:
    """Row-level tenant guard built on top of the repository scope key."""

    def __init__(self, default_tenant: str | None = None) -> None:
        self._default = default_tenant or get_settings().default_tenant_id

    def resolve(self, tenant_id: str | None) -> str:
        """Resolve a request's tenant, falling back to the ContextVar + default."""
        if tenant_id:
            return tenant_id
        fallback = get_current_tenant()
        return fallback or self._default

    def assert_access(self, requester_tenant: str | None, target_tenant: str | None) -> None:
        """Raise ``PermissionError`` if the two tenants differ."""
        if self.resolve(requester_tenant) != self.resolve(target_tenant):
            raise PermissionError("cross-tenant access denied")

    def can_access(self, requester_tenant: str | None, target_tenant: str | None) -> bool:
        return self.resolve(requester_tenant) == self.resolve(target_tenant)

    def filter_rows(self, rows: Iterable[Any], tenant_id: str | None) -> list[Any]:
        """Keep only rows owned by ``tenant_id`` (defensive belt-and-braces)."""
        scope = self.resolve(tenant_id)
        kept: list[Any] = []
        for row in rows:
            row_tenant = row.get("tenant_id") if isinstance(row, dict) else getattr(row, "tenant_id", None)
            if self.resolve(row_tenant) == scope:
                kept.append(row)
        return kept


def assert_tenant(requester_tenant: str | None, target_tenant: str | None) -> None:
    """Functional shortcut used by routers for write-path 403s."""
    TenantIsolation().assert_access(requester_tenant, target_tenant)
