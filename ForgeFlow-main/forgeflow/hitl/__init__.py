"""INC46 T21 — the generic "pause for a human" (HITL) primitive.

One mechanism (:class:`PendingAction`) shared by all three human-in-the-loop
situations — ``clarify`` (ask a question), ``approve_diff`` (approve a candidate
new version of a user artefact) and ``confirm_permission`` (confirm an
out-of-band / external action). The state is **persisted** (migration ``025``,
``pending_actions``), tenant scoped, resolvable after a process restart, and
**fail-closed** on timeout: an expired action never becomes an implicit "yes".

Public surface
--------------
* :class:`PendingAction` — the record (e.g. ``PendingAction`` dataclass).
* :class:`PendingActionStore` — the shared store interface (in-memory + PG).
* :func:`is_expired`, :func:`decide_permission_level` — the two pure deciders.
* :mod:`forgeflow.hitl.policy` — the four-level → :class:`PendingAction` map.
"""

from __future__ import annotations

from forgeflow.hitl.pending import (
    APPROVE_DIFF,
    CLARIFY,
    CONFIRM_PERMISSION,
    DEFAULT_TIMEOUT_HOURS,
    EXPIRED,
    PENDING_KINDS,
    PENDING_STATUSES,
    RESOLVED,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_EXPIRED,
    RUN_STATUS_PAUSED,
    RUN_STATUS_RESUMED,
    TERMINAL_PENDING_STATUSES,
    WAITING,
    CANCELLED,
    CrossTenantPendingAction,
    InMemoryPendingActionStore,
    PendingAction,
    PendingActionExpired,
    PendingActionNotFound,
    PendingActionNotWaiting,
    PendingActionStore,
    PostgresPendingActionStore,
    decide_permission_level,
    default_expiry,
    get_pending_store,
    is_expired,
    reset_pending_store,
    run_status_for,
    set_pending_store,
)

__all__ = [
    "APPROVE_DIFF",
    "CLARIFY",
    "CONFIRM_PERMISSION",
    "DEFAULT_TIMEOUT_HOURS",
    "EXPIRED",
    "PENDING_KINDS",
    "PENDING_STATUSES",
    "RESOLVED",
    "RUN_STATUS_CANCELLED",
    "RUN_STATUS_EXPIRED",
    "RUN_STATUS_PAUSED",
    "RUN_STATUS_RESUMED",
    "TERMINAL_PENDING_STATUSES",
    "WAITING",
    "CANCELLED",
    "CrossTenantPendingAction",
    "InMemoryPendingActionStore",
    "PendingAction",
    "PendingActionExpired",
    "PendingActionNotFound",
    "PendingActionNotWaiting",
    "PendingActionStore",
    "PostgresPendingActionStore",
    "decide_permission_level",
    "default_expiry",
    "get_pending_store",
    "is_expired",
    "reset_pending_store",
    "run_status_for",
    "set_pending_store",
]
