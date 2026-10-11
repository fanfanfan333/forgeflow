"""Transaction-local tenant context shared by HTTP and background work."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

_tenant: ContextVar[str | None] = ContextVar("forgeflow_tenant", default=None)


def current_tenant() -> str | None:
    return _tenant.get()


@contextmanager
def tenant_scope(tenant_id: str | None):
    token = _tenant.set(tenant_id)
    try:
        yield
    finally:
        _tenant.reset(token)
