"""Tenant context — a ``ContextVar`` fallback for cross-layer calls (docs §10.5).

Explicit ``tenant_id`` parameters are always preferred; this ContextVar exists
only so deep helpers (DlpGate, PolicyEngine) can read a sane default when the
caller can't thread the value through. It is never used to *authorize* a read —
row-level filtering still comes from the explicit parameter.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

_current_tenant: ContextVar[str | None] = ContextVar("agentflow_current_tenant", default=None)
_current_actor: ContextVar[str | None] = ContextVar("agentflow_current_actor", default=None)


def get_current_tenant() -> str | None:
    return _current_tenant.get()


def set_current_tenant(tenant_id: str | None) -> Token:
    return _current_tenant.set(tenant_id)


def reset_current_tenant(token: Token) -> None:
    _current_tenant.reset(token)


def get_current_actor() -> str | None:
    return _current_actor.get()


def set_current_actor(actor: str | None) -> Token:
    return _current_actor.set(actor)


def reset_current_actor(token: Token) -> None:
    _current_actor.reset(token)
