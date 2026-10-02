"""BE-5 — the single mandatory tenant-scope gate for skill assets.

Tenant isolation is a **security property**, not a UI convenience. INC43 §3.4
places it *below* RBAC — at the repository/service boundary — so that neither a
route handler, a front-end caller, nor an ``admin`` role can widen it. The two
helpers here are the *only* sanctioned way to (a) assert that a request carries a
resolved tenant before a skill asset is read or selected, and (b) decide whether
a stored record belongs to the caller's tenant.

Design rules (fail-closed by construction):

* :func:`require_tenant` **raises** ``GovernanceError(403)`` on an empty/``None``
  tenant instead of returning a sentinel — a caller that forgets to check cannot
  silently proceed against "all tenants".
* :func:`scope_filter` is strict equality. A record whose ``tenant_id`` is
  ``None`` matches **no** tenant (it belongs to no session), so it can never be
  surfaced through a tenant-scoped query — the opposite of the historical
  "``None`` ⇒ global" leak.

Both helpers are pure (no I/O, no LLM) so they are safe to call on every read
path and trivially unit-testable.
"""

from __future__ import annotations

from forgeflow.skills.errors import GovernanceError

__all__ = ["require_tenant", "scope_filter"]


def require_tenant(tenant: str | None) -> str:
    """Assert a resolved tenant and return it, or fail closed with 403.

    Args:
        tenant: the tenant id resolved from the authenticated session. Empty
            string and ``None`` are treated identically.

    Returns:
        The same non-empty ``tenant`` string, so callers can write
        ``tenant = require_tenant(tenant)``.

    Raises:
        GovernanceError: ``status_code == 403`` when ``tenant`` is falsy.
    """
    if not tenant:
        raise GovernanceError(
            "tenant context required for skill discovery/selection",
            status_code=403,
        )
    return tenant


def scope_filter(record_tenant: str | None, tenant: str) -> bool:
    """Return whether a stored record belongs to ``tenant`` (strict equality).

    A ``None``/empty ``record_tenant`` matches **no** tenant, so legacy or
    global rows never leak into a tenant-scoped result set.

    Args:
        record_tenant: the ``tenant_id`` carried by the stored record.
        tenant: the caller's resolved tenant (must already be non-empty; see
            :func:`require_tenant`).

    Returns:
        ``True`` iff ``record_tenant`` is not ``None`` and equals ``tenant``.
    """
    return record_tenant is not None and record_tenant == tenant
