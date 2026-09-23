"""Organizational Memory promotion / demotion (INC2 A6, §2.6).

A memory fact can be *promoted* up the five-layer hierarchy (``user`` → ``team``
→ ``org``) so a single person's or team's knowledge becomes reusable org-wide.
Promotion is an explicit, audited operation:

  * Only scopes marked ``promotable`` in ``experience.scopes.SCOPE_RULES`` can be
    the *source* (``EPISODIC`` and ``ORG`` cannot be promoted — the former is a
    run trace, the latter is already the top layer).
  * The *target* must be ``team`` or ``org``.
  * Promoting **to org** requires an ``admin`` role.
  * The namespace is recomputed by ``memory_store.move`` and every promotion
    writes an ``AuditEvent(action="memory.promote")``.

This module never mutates ``experience/scopes.py`` — it consumes its rules.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from forgeflow.experience.memory_store import MemoryEntry, get_memory, move_memory
from forgeflow.experience.scopes import SCOPE_RULES, MemoryScope, is_valid_scope

logger = logging.getLogger(__name__)

__all__ = [
    "PromotionError",
    "PromotionAuditEvent",
    "PROMOTABLE_TARGETS",
    "promote_memory",
    "list_promotion_audit",
    "clear_promotion_audit",
]

# A promotion target must be one of these two layers.
PROMOTABLE_TARGETS: frozenset[str] = frozenset(
    {MemoryScope.TEAM.value, MemoryScope.ORG.value}
)

_ADMIN_ROLE = "admin"


class PromotionError(Exception):
    """Raised when a promotion request is invalid or not permitted."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass(frozen=True)
class PromotionAuditEvent:
    """Immutable record of one promotion (offline audit sink)."""

    action: str
    actor: str | None
    tenant_id: str | None
    resource: str
    metadata: dict[str, Any]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "actor": self.actor,
            "tenant_id": self.tenant_id,
            "resource": self.resource,
            "metadata": dict(self.metadata),
            "created_at": self.created_at.isoformat(),
        }


# In-process audit sink. The public API layers a request-scoped audit on top;
# this guarantees the promotion is always recorded even with no PostgreSQL.
_AUDIT: list[PromotionAuditEvent] = []


def list_promotion_audit() -> list[PromotionAuditEvent]:
    """Return the recorded promotion audit events (newest last)."""
    return list(_AUDIT)


def clear_promotion_audit() -> None:
    """Test helper: drop the recorded events."""
    _AUDIT.clear()


def _write_audit(
    *,
    tenant_id: str | None,
    memory_id: str,
    actor_id: str | None,
    from_scope: str,
    to_scope: str,
    reason: str,
) -> PromotionAuditEvent:
    event = PromotionAuditEvent(
        action="memory.promote",
        actor=actor_id,
        tenant_id=tenant_id,
        resource=memory_id,
        metadata={"from": from_scope, "to": to_scope, "reason": reason},
    )
    _AUDIT.append(event)
    logger.info(
        "memory.promote | tenant=%s id=%s %s→%s actor=%s",
        tenant_id,
        memory_id,
        from_scope,
        to_scope,
        actor_id,
    )
    return event


def _is_promotable(scope: str) -> bool:
    rule = SCOPE_RULES.get(scope)
    return bool(rule and rule.get("promotable"))


async def promote_memory(
    tenant_id: str | None,
    memory_id: str,
    from_scope: str | None = None,
    to_scope: str = MemoryScope.TEAM.value,
    *,
    actor_id: str | None = None,
    role: str = "viewer",
    reason: str = "",
    team_id: str | None = None,
) -> MemoryEntry:
    """Promote a memory fact upward, enforcing the promotable / admin rules.

    Raises :class:`PromotionError` (with a ``status_code``) on any violation, so
    the API layer can map it to 400/403/404 directly.
    """
    entry = await get_memory(tenant_id, memory_id)
    if entry is None:
        raise PromotionError(f"memory {memory_id} not found", status_code=404)

    source = from_scope or entry.scope
    if not is_valid_scope(source):
        raise PromotionError(f"unknown source scope '{source}'", status_code=400)
    if from_scope and from_scope != entry.scope:
        raise PromotionError(
            f"from_scope '{from_scope}' does not match stored scope '{entry.scope}'",
            status_code=400,
        )
    if not _is_promotable(source):
        raise PromotionError(
            f"scope '{source}' is not promotable (EPISODIC/ORG cannot be promoted)",
            status_code=400,
        )
    if to_scope not in PROMOTABLE_TARGETS:
        raise PromotionError(
            f"to_scope must be one of {sorted(PROMOTABLE_TARGETS)}", status_code=400
        )
    if source == to_scope:
        raise PromotionError("from_scope and to_scope are identical", status_code=400)
    if to_scope == MemoryScope.ORG.value and role != _ADMIN_ROLE:
        raise PromotionError(
            "promoting to the org layer requires the admin role", status_code=403
        )

    moved = await move_memory(
        tenant_id, memory_id, to_scope, team_id=team_id, actor_id=actor_id
    )
    if moved is None:  # pragma: no cover - entry verified above; defensive
        raise PromotionError(f"memory {memory_id} not found", status_code=404)

    _write_audit(
        tenant_id=tenant_id,
        memory_id=memory_id,
        actor_id=actor_id,
        from_scope=source,
        to_scope=to_scope,
        reason=reason,
    )
    return moved
