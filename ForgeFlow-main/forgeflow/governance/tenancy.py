"""Tenant isolation helpers (docs §6.1 / P0-10).

``TenantIsolation`` centralises the row-level rules so every hub enforces them
the same way: reads that miss the tenant return empty (→ 404 upstream, never
revealing existence), writes that cross tenants raise ``PermissionError``
(→ 403).
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
