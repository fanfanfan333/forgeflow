"""INC46 T21 — :class:`PendingAction` + the pending-action stores (migration 025).

One mechanism for every "the run must stop and wait for a person" moment:

* ``clarify``            — the agent needs an answer to continue;
* ``approve_diff``       — a user artefact is about to get a **new version**
                           (WRITE) and a human approves the diff;
* ``confirm_permission`` — an out-of-band / external action (EXTERNAL) or a
                           per-instance DANGEROUS confirmation.

Splitting this file
-------------------
* :class:`PendingAction` — the frozen record; the fields the task book pins
  (``pending_id`` / ``run_id`` / ``kind`` / ``payload`` / ``status`` /
  ``expires_at``) plus the storage-scope fields (``tenant_id`` and the
  resolution bookkeeping).
* :func:`is_expired` and :func:`decide_permission_level` — the two **pure**
  deciders (no I/O), trivially unit-testable.
* :class:`PendingActionStore` — the shared store logic (idempotent resolve,
  fail-closed expiry, cross-tenant refusal) as template methods; two concrete
  backends implement the four storage primitives:

  - :class:`InMemoryPendingActionStore` — process-local dicts (offline profile);
  - :class:`PostgresPendingActionStore` — the real ``pending_actions`` table
    (``psycopg`` sync, mirroring :class:`forgeflow.outcomes.store.PostgresOutcomeStore`).

Storage discipline (house rules)
--------------------------------
* ``tenant_id`` is the **first positional argument** of every read/write so
  row-level isolation cannot be forgotten. A falsy tenant reads as the **empty
  set** (fail-closed, 红线 5) and refuses to write.
* ``None`` means **not measured / not configured** — ``payload`` may be NULL and
  ``resolution``/``resolved_at``/``resolved_by`` stay ``None`` until a human acts.
  Nothing is ever coerced to ``0`` / ``""`` (红线 4).
* An action is **never** auto-approved by the passage of time: past
  ``expires_at`` it becomes ``expired`` and any resolve raises
  :class:`PendingActionExpired` (超时 fail-closed, 红线 21).

A run whose pending action expires is left in the additive ``expired`` run
status (:data:`RUN_STATUS_EXPIRED`); the existing run-status vocabulary in
``forgeflow.workspace.store`` is **not** changed (红线 1 — additive only).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from forgeflow.repositories.base import new_id, utcnow

logger = logging.getLogger(__name__)

__all__ = [
    # kinds
    "CLARIFY",
    "APPROVE_DIFF",
    "CONFIRM_PERMISSION",
    "PENDING_KINDS",
    # statuses
    "WAITING",
    "RESOLVED",
    "EXPIRED",
    "CANCELLED",
    "PENDING_STATUSES",
    "TERMINAL_PENDING_STATUSES",
    # run-status (additive)
    "RUN_STATUS_PAUSED",
    "RUN_STATUS_RESUMED",
    "RUN_STATUS_EXPIRED",
    "RUN_STATUS_CANCELLED",
    # decisions
    "READ",
    "WRITE",
    "EXTERNAL",
    "DANGEROUS",
    "ACTION_AUTO",
    "ACTION_PENDING_APPROVAL",
    "ACTION_CONFIRM_PERMISSION",
    "ACTION_DENY",
    # timeout
    "DEFAULT_TIMEOUT_HOURS",
    "DEFAULT_TIMEOUT_SECONDS",
    "default_expiry",
    "is_expired",
    "decide_permission_level",
    # record + errors
    "PendingAction",
    "PendingActionError",
    "PendingActionNotFound",
    "CrossTenantPendingAction",
    "PendingActionExpired",
    "PendingActionNotWaiting",
    # stores
    "PendingActionStore",
    "InMemoryPendingActionStore",
    "PostgresPendingActionStore",
    "get_pending_store",
    "set_pending_store",
    "reset_pending_store",
]

# --------------------------------------------------------------------------- #
# Vocabulary                                                                   #
# --------------------------------------------------------------------------- #
# ``kind`` ∈ {clarify, approve_diff, confirm_permission}
CLARIFY = "clarify"
APPROVE_DIFF = "approve_diff"
CONFIRM_PERMISSION = "confirm_permission"
PENDING_KINDS: tuple[str, ...] = (CLARIFY, APPROVE_DIFF, CONFIRM_PERMISSION)

# ``status`` ∈ {waiting, resolved, expired, cancelled}
WAITING = "waiting"
RESOLVED = "resolved"
EXPIRED = "expired"
CANCELLED = "cancelled"
PENDING_STATUSES: tuple[str, ...] = (WAITING, RESOLVED, EXPIRED, CANCELLED)
#: A resolved / expired / cancelled action is done — a second resolve is a no-op
#: (idempotent) or a refusal, never a second decision.
TERMINAL_PENDING_STATUSES: frozenset[str] = frozenset({RESOLVED, EXPIRED, CANCELLED})

# Run-status vocabulary — **additive**. ``forgeflow.workspace.store`` keeps its
# own ``TERMINAL_STATUSES`` untouched; these are the HITL-side labels a run takes
# while it waits for / after a human decision.
RUN_STATUS_PAUSED = "paused"
RUN_STATUS_RESUMED = "resumed"
RUN_STATUS_EXPIRED = "expired"
RUN_STATUS_CANCELLED = "cancelled"

_RUN_STATUS_BY_PENDING: dict[str, str] = {
    WAITING: RUN_STATUS_PAUSED,
    RESOLVED: RUN_STATUS_RESUMED,
    EXPIRED: RUN_STATUS_EXPIRED,
    CANCELLED: RUN_STATUS_CANCELLED,
}

# Four-level privilege model (re-used from the T04 classifier — never re-invented).
READ = "READ"
WRITE = "WRITE"
EXTERNAL = "EXTERNAL"
DANGEROUS = "DANGEROUS"

# The four outcomes :func:`decide_permission_level` can return.
ACTION_AUTO = "auto"
ACTION_PENDING_APPROVAL = "pending_approval"
ACTION_CONFIRM_PERMISSION = "confirm_permission"
ACTION_DENY = "deny"

#: Default time a run waits for a human before the action is declared ``expired``.
DEFAULT_TIMEOUT_HOURS = 24
DEFAULT_TIMEOUT_SECONDS = DEFAULT_TIMEOUT_HOURS * 3600


def _now() -> datetime:
    """Timezone-aware UTC now (one clock, mirrors the repositories base)."""
    return datetime.now(timezone.utc)


def default_expiry(
    now: datetime | None = None,
    timeout_hours: int = DEFAULT_TIMEOUT_HOURS,
) -> datetime:
    """The default deadline: ``now + timeout_hours`` (default 24h)."""
    return (now or _now()) + timedelta(hours=timeout_hours)


def is_expired(now: datetime, expires_at: datetime | None) -> bool:
    """Return whether a deadline has passed.

    A ``None`` deadline means "no deadline configured" (未配置 ⇒ ``None``, 红线 4)
    and therefore never expires — it is **not** treated as an immediate expiry and
    **not** treated as an implicit approval.
    """
    if expires_at is None:
        return False
    return now >= expires_at


def decide_permission_level(
    level: str,
    *,
    tenant_policy: dict[str, Any] | None = None,
    is_admin_policy: bool = False,
) -> dict[str, Any]:
    """Map a four-level tool class to the HITL action it requires (pure).

    The four classes come from :mod:`forgeflow.skills.tool_permissions` (T04) —
    this function only decides *what the run must do*, it never re-classifies.

    Args:
        level: one of ``READ`` / ``WRITE`` / ``EXTERNAL`` / ``DANGEROUS``.
        tenant_policy: the tenant's HITL policy. The only recognised key today is
            ``{"auto_approve_low_risk": True}`` which lets a **WRITE** run without
            a human — the caller **must** record an audit entry for that shortcut
            (see :func:`forgeflow.hitl.policy.build_audit_record`).
        is_admin_policy: whether an explicit **admin** policy authorises this
            DANGEROUS tool. Anything other than a truthy admin policy leaves a
            DANGEROUS tool **denied**.

    Returns:
        ``{"action": <auto|pending_approval|confirm_permission|deny>,
        "kind": <clarify|approve_diff|confirm_permission|None>, "reason": str}``.

        * ``READ``      ⇒ ``auto`` (no pause).
        * ``WRITE``     ⇒ ``pending_approval`` / ``approve_diff`` — unless the
          tenant's low-risk auto-approve is on ⇒ ``auto`` (audited).
        * ``EXTERNAL``  ⇒ ``confirm_permission`` / ``confirm_permission``.
        * ``DANGEROUS`` ⇒ ``deny`` (no pending produced) unless an explicit admin
          policy is present ⇒ ``confirm_permission`` (逐次确认).
        * anything else ⇒ ``deny`` (fail closed).
    """
    policy = tenant_policy or {}
    auto_low_risk = bool(policy.get("auto_approve_low_risk"))

    if level == READ:
        return {"action": ACTION_AUTO, "kind": None, "reason": "read-only: no human needed"}

    if level == WRITE:
        if auto_low_risk:
            return {
                "action": ACTION_AUTO,
                "kind": None,
                "reason": "tenant policy auto-approves low-risk writes (audited)",
            }
        return {
            "action": ACTION_PENDING_APPROVAL,
            "kind": APPROVE_DIFF,
            "reason": "write creates a new artefact version: diff approval required",
        }

    if level == EXTERNAL:
        return {
            "action": ACTION_CONFIRM_PERMISSION,
            "kind": CONFIRM_PERMISSION,
            "reason": "external egress: explicit permission confirmation required",
        }

    if level == DANGEROUS:
        if not is_admin_policy:
            return {
                "action": ACTION_DENY,
                "kind": None,
                "reason": "dangerous tool denied by default (no admin policy)",
            }
        return {
            "action": ACTION_CONFIRM_PERMISSION,
            "kind": CONFIRM_PERMISSION,
            "reason": "admin policy authorises, but every instance is confirmed",
        }

    return {
        "action": ACTION_DENY,
        "kind": None,
        "reason": f"unknown privilege level {level!r}: denied (fail closed)",
    }


def run_status_for(pending_status: str) -> str:
    """The run status that corresponds to a pending-action status.

    ``waiting`` ⇒ ``paused``; ``resolved`` ⇒ ``resumed``; ``expired`` ⇒
    ``expired``; ``cancelled`` ⇒ ``cancelled``. An unknown status fails closed to
    ``expired`` (never "resumed"/"approved").
    """
    return _RUN_STATUS_BY_PENDING.get(pending_status, RUN_STATUS_EXPIRED)


# --------------------------------------------------------------------------- #
# Record                                                                       #
# --------------------------------------------------------------------------- #
@dataclass
class PendingAction:
    """A persisted "waiting for a human" record.

    Attributes:
        pending_id: opaque id (``new_id()`` UUID by default).
        run_id: the run that paused.
        kind: ``clarify`` / ``approve_diff`` / ``confirm_permission``.
        payload: the human-facing context (question / diff / permission detail).
            ``{}`` when nothing was supplied — never ``None`` on the record.
        status: ``waiting`` / ``resolved`` / ``expired`` / ``cancelled``.
        expires_at: the deadline; ``None`` when no deadline was configured.
        tenant_id: the owning tenant (storage scope). ``None`` only transiently,
            before a scoped write assigns it.
        resolution: the human's decision text; ``None`` until resolved.
        resolved_at: when it was resolved; ``None`` until resolved.
        resolved_by: who resolved it; ``None`` until resolved (or unknown).
        created_at: when the record was created.
    """

    pending_id: str
    run_id: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = WAITING
    expires_at: datetime | None = None
    tenant_id: str | None = None
    resolution: str | None = None
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    created_at: datetime | None = None

    @property
    def is_terminal(self) -> bool:
        """Whether this action will no longer change on its own."""
        return self.status in TERMINAL_PENDING_STATUSES

    @property
    def run_status(self) -> str:
        """The run status implied by this action's status."""
        return run_status_for(self.status)

    @property
    def run_continues(self) -> bool:
        """Whether a human resolve lets the run continue (i.e. it is resolved)."""
        return self.status == RESOLVED

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly view (datetimes as ISO-8601, ``None`` preserved)."""
        return {
            "pending_id": self.pending_id,
            "run_id": self.run_id,
            "kind": self.kind,
            "payload": dict(self.payload or {}),
            "status": self.status,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "tenant_id": self.tenant_id,
            "resolution": self.resolution,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "resolved_by": self.resolved_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "run_status": self.run_status,
            "run_continues": self.run_continues,
        }


# --------------------------------------------------------------------------- #
# Errors                                                                       #
# --------------------------------------------------------------------------- #
class PendingActionError(Exception):
    """Base class for every pending-action failure (⇒ mapped to an HTTP status)."""


class PendingActionNotFound(PendingActionError):
    """No such action for this tenant (⇒ 404)."""

    def __init__(self, pending_id: str) -> None:
        super().__init__(f"pending action {pending_id} not found")
        self.pending_id = pending_id


class CrossTenantPendingAction(PendingActionError):
    """The action belongs to **another** tenant (⇒ 403). Never a shadow write."""

    def __init__(self, pending_id: str) -> None:
        super().__init__(f"pending action {pending_id} belongs to another tenant")
        self.pending_id = pending_id


class PendingActionExpired(PendingActionError):
    """The action expired (⇒ 409). Fail closed — never an implicit approval."""

    def __init__(self, pending_id: str) -> None:
        super().__init__(f"pending action {pending_id} expired (fail-closed)")
        self.pending_id = pending_id


class PendingActionNotWaiting(PendingActionError):
    """The action is terminal but not resolved (e.g. cancelled) (⇒ 409)."""

    def __init__(self, pending_id: str, status: str) -> None:
        super().__init__(f"pending action {pending_id} is {status}, not waiting")
        self.pending_id = pending_id
        self.status = status


# --------------------------------------------------------------------------- #
# Store — shared logic (template methods over four storage primitives)         #
# --------------------------------------------------------------------------- #
class PendingActionStore:
    """Tenant-scoped store for :class:`PendingAction` records.

    This base class owns every *behavioural* rule so both backends behave
    identically:

    * fail-closed reads on a falsy tenant (empty set);
    * refuse to write without a tenant;
    * **idempotent** resolve (a second resolve returns the first result);
    * **fail-closed** expiry (past ``expires_at`` ⇒ ``expired`` and a resolve
      raises :class:`PendingActionExpired`);
    * cross-tenant refusal (:class:`CrossTenantPendingAction`) — never a shadow
      copy under the caller's tenant.

    Subclasses implement the four storage primitives :meth:`_load`,
    :meth:`_load_any`, :meth:`_scan` and :meth:`_save` over a canonical *row*
    ``dict`` (keys documented in :meth:`_row_to_action`).
    """

    # --- storage primitives (implemented per backend) ---------------------- #
    def _load(self, tenant: str | None, pending_id: str) -> dict[str, Any] | None:
        """Tenant-scoped row, or ``None``. Must return ``None`` for a falsy tenant."""
        raise NotImplementedError

    def _load_any(self, pending_id: str) -> dict[str, Any] | None:
        """Row for ``pending_id`` regardless of tenant (cross-tenant detection)."""
        raise NotImplementedError

    def _scan(
        self,
        tenant: str | None,
        *,
        run_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Rows for ``tenant`` (newest first), optionally filtered."""
        raise NotImplementedError

    def _save(self, row: dict[str, Any]) -> None:
        """Upsert ``row`` by ``(tenant_id, pending_id)``."""
        raise NotImplementedError

    # --- row ⇄ record ------------------------------------------------------- #
    @staticmethod
    def _row_to_action(row: dict[str, Any]) -> PendingAction:
        """Convert a canonical row dict into a :class:`PendingAction`."""
        payload = row.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                payload = {}
        return PendingAction(
            pending_id=str(row["pending_id"]),
            run_id=str(row["run_id"]),
            kind=str(row["kind"]),
            payload=dict(payload) if isinstance(payload, dict) else {},
            status=str(row["status"]),
            expires_at=row.get("expires_at"),
            tenant_id=(str(row["tenant_id"]) if row.get("tenant_id") else None),
            resolution=row.get("resolution"),
            resolved_at=row.get("resolved_at"),
            resolved_by=row.get("resolved_by"),
            created_at=row.get("created_at"),
        )

    @staticmethod
    def _action_to_row(action: PendingAction, tenant: str) -> dict[str, Any]:
        """Convert a :class:`PendingAction` into a canonical row dict."""
        return {
            "tenant_id": tenant,
            "pending_id": action.pending_id,
            "run_id": action.run_id,
            "kind": action.kind,
            "payload": dict(action.payload or {}),
            "status": action.status,
            "resolution": action.resolution,
            "expires_at": action.expires_at,
            "resolved_at": action.resolved_at,
            "resolved_by": action.resolved_by,
            "created_at": action.created_at or _now(),
        }

    # --- writes ------------------------------------------------------------- #
    def create(
        self,
        tenant: str | None,
        *,
        run_id: str,
        kind: str,
        payload: dict[str, Any] | None = None,
        pending_id: str | None = None,
        expires_at: datetime | None = None,
        timeout_hours: int = DEFAULT_TIMEOUT_HOURS,
        status: str = WAITING,
        now: datetime | None = None,
    ) -> PendingAction:
        """Persist a new pending action and return it.

        Raises:
            ValueError: when ``tenant`` is falsy (an unscoped write must never
                happen — 红线 5) or ``kind`` is not one of :data:`PENDING_KINDS`.
        """
        if not tenant:
            raise ValueError("tenant required to create a pending action (fail closed)")
        if kind not in PENDING_KINDS:
            raise ValueError(f"unknown pending kind {kind!r}; expected {PENDING_KINDS}")

        moment = now or _now()
        record = PendingAction(
            pending_id=pending_id or f"pa-{new_id()}",
            run_id=run_id,
            kind=kind,
            payload=dict(payload or {}),
            status=status,
            expires_at=expires_at if expires_at is not None else default_expiry(moment, timeout_hours),
            tenant_id=tenant,
            created_at=moment,
        )
        self._save(self._action_to_row(record, tenant))
        logger.info(
            "pending action created | tenant=%s pending=%s run=%s kind=%s expires=%s",
            tenant, record.pending_id, run_id, kind, record.expires_at,
        )
        return record

    def resolve(
        self,
        tenant: str | None,
        pending_id: str,
        *,
        decision: str = "approve",
        actor: str | None = None,
        now: datetime | None = None,
    ) -> PendingAction:
        """Record a human decision and return the resolved action.

        Idempotent: resolving an already-resolved action returns the **first**
        result unchanged (no double count).

        Raises:
            PendingActionNotFound: no such action for this tenant (⇒ 404).
            CrossTenantPendingAction: the action belongs to another tenant (⇒ 403).
            PendingActionExpired: the action expired — fail closed (⇒ 409).
            PendingActionNotWaiting: the action is cancelled/terminal (⇒ 409).
        """
        if not tenant:
            raise PendingActionNotFound(pending_id)

        row = self._load(tenant, pending_id)
        if row is None:
            if self._load_any(pending_id) is not None:
                raise CrossTenantPendingAction(pending_id)
            raise PendingActionNotFound(pending_id)

        row = self._maybe_expire(row, now)
        status = str(row["status"])
        if status == EXPIRED:
            raise PendingActionExpired(pending_id)
        if status == RESOLVED:
            # Idempotent: return the first resolution, do not overwrite.
            return self._row_to_action(row)
        if status != WAITING:
            raise PendingActionNotWaiting(pending_id, status)

        moment = now or _now()
        row["status"] = RESOLVED
        row["resolution"] = decision
        row["resolved_at"] = moment
        row["resolved_by"] = actor
        self._save(row)
        logger.info(
            "pending action resolved | tenant=%s pending=%s decision=%s by=%s",
            tenant, pending_id, decision, actor,
        )
        return self._row_to_action(row)

    def cancel(
        self,
        tenant: str | None,
        pending_id: str,
        *,
        now: datetime | None = None,
    ) -> PendingAction:
        """Cancel a waiting action (idempotent for an already-terminal record)."""
        if not tenant:
            raise PendingActionNotFound(pending_id)
        row = self._load(tenant, pending_id)
        if row is None:
            if self._load_any(pending_id) is not None:
                raise CrossTenantPendingAction(pending_id)
            raise PendingActionNotFound(pending_id)
        if str(row["status"]) in TERMINAL_PENDING_STATUSES:
            return self._row_to_action(row)
        row["status"] = CANCELLED
        row["resolved_at"] = now or _now()
        self._save(row)
        return self._row_to_action(row)

    def expire_overdue(
        self, tenant: str | None, *, now: datetime | None = None
    ) -> list[PendingAction]:
        """Flip every overdue ``waiting`` action to ``expired``; return those flipped.

        The sweep is tenant scoped. Each newly-expired action's run is left in the
        additive ``expired`` run status (:data:`RUN_STATUS_EXPIRED`) — never
        approved (超时 fail-closed).
        """
        moment = now or _now()
        flipped: list[PendingAction] = []
        for row in self._scan(tenant, status=WAITING):
            if is_expired(moment, row.get("expires_at")):
                row["status"] = EXPIRED
                self._save(row)
                flipped.append(self._row_to_action(row))
        return flipped

    # --- reads -------------------------------------------------------------- #
    def get(self, tenant: str | None, pending_id: str) -> PendingAction | None:
        """Fetch one action, tenant scoped. ``None`` for an unknown/foreign action."""
        if not tenant:
            return None
        row = self._load(tenant, pending_id)
        if row is None:
            return None
        return self._row_to_action(self._maybe_expire(row, None))

    def list(
        self,
        tenant: str | None,
        *,
        run_id: str | None = None,
        status: str | None = None,
    ) -> list[PendingAction]:
        """List a tenant's actions (newest first), optionally filtered.

        A falsy tenant yields the **empty set** (fail-closed read, 红线 5).
        """
        if not tenant:
            return []
        rows = self._scan(tenant, run_id=run_id, status=status)
        return [self._row_to_action(row) for row in rows]

    # --- shared helper ------------------------------------------------------ #
    def _maybe_expire(
        self, row: dict[str, Any], now: datetime | None
    ) -> dict[str, Any]:
        """Flip a still-``waiting`` overdue row to ``expired`` and persist it."""
        if str(row.get("status")) == WAITING and is_expired(now or _now(), row.get("expires_at")):
            row["status"] = EXPIRED
            self._save(row)
        return row


# --------------------------------------------------------------------------- #
# In-memory backend (offline profile)                                          #
# --------------------------------------------------------------------------- #
class InMemoryPendingActionStore(PendingActionStore):
    """Process-local dict store (the offline profile; a restart drops it).

    Deliberately mirrors the PG semantics exactly so the unit suite can run with
    no database while the integration suite proves the real table — the same
    split :class:`forgeflow.outcomes.store` uses. Because a restart drops this
    store, it is **not** a valid persistence backend (the restart-recovery case
    lives in ``tests/integration/test_inc46_hitl_pg.py``).
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], dict[str, Any]] = {}

    def _load(self, tenant: str | None, pending_id: str) -> dict[str, Any] | None:
        if not tenant:
            return None
        row = self._rows.get((tenant, pending_id))
        return dict(row) if row is not None else None

    def _load_any(self, pending_id: str) -> dict[str, Any] | None:
        for (_tenant, pid), row in self._rows.items():
            if pid == pending_id:
                return dict(row)
        return None

    def _scan(
        self,
        tenant: str | None,
        *,
        run_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        if not tenant:
            return []
        rows = [dict(r) for (t, _pid), r in self._rows.items() if t == tenant]
        if run_id is not None:
            rows = [r for r in rows if r.get("run_id") == run_id]
        if status is not None:
            rows = [r for r in rows if r.get("status") == status]
        rows.sort(key=lambda r: r.get("created_at") or _now(), reverse=True)
        return rows

    def _save(self, row: dict[str, Any]) -> None:
        tenant = str(row["tenant_id"])
        self._rows[(tenant, str(row["pending_id"]))] = dict(row)


# --------------------------------------------------------------------------- #
# PostgreSQL backend (real ``pending_actions`` table)                          #
# --------------------------------------------------------------------------- #
def _normalise_dsn(dsn: str) -> str:
    """Strip the SQLAlchemy driver suffix so ``psycopg`` can parse the DSN."""
    return dsn.replace("postgresql+psycopg://", "postgresql://")


def _coerce_payload(value: Any) -> Any:
    """Decode a JSONB column that may arrive as ``dict``/``list`` or JSON text."""
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value


class PostgresPendingActionStore(PendingActionStore):
    """The real ``pending_actions`` table (migration ``025``) via ``psycopg``.

    Synchronous by design, mirroring
    :class:`forgeflow.outcomes.store.PostgresOutcomeStore`: the async router calls
    it directly, and the integration suite constructs it with an explicit DSN so
    it can run without the async pool.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = _normalise_dsn(dsn)

    def _connect(self):  # noqa: ANN202 — psycopg connection, imported lazily
        import psycopg

        return psycopg.connect(self._dsn)

    def _load(self, tenant: str | None, pending_id: str) -> dict[str, Any] | None:
        if not tenant:
            return None
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT tenant_id, pending_id, run_id, kind, payload, status, "
                "resolution, expires_at, resolved_at, resolved_by, created_at "
                "FROM pending_actions WHERE tenant_id = %s AND pending_id = %s",
                (tenant, pending_id),
            )
            row = cur.fetchone()
        return self._row(row)

    def _load_any(self, pending_id: str) -> dict[str, Any] | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT tenant_id, pending_id, run_id, kind, payload, status, "
                "resolution, expires_at, resolved_at, resolved_by, created_at "
                "FROM pending_actions WHERE pending_id = %s LIMIT 1",
                (pending_id,),
            )
            row = cur.fetchone()
        return self._row(row)

    @staticmethod
    def _row(row: Any) -> dict[str, Any] | None:  # noqa: ANN401
        if row is None:
            return None
        names = ("tenant_id", "pending_id", "run_id", "kind", "payload", "status",
                 "resolution", "expires_at", "resolved_at", "resolved_by", "created_at")
        data = dict(zip(names, row))
        data["payload"] = _coerce_payload(data.get("payload"))
        return data

    def _scan(
        self,
        tenant: str | None,
        *,
        run_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        if not tenant:
            return []
        sql = (
            "SELECT tenant_id, pending_id, run_id, kind, payload, status, "
            "resolution, expires_at, resolved_at, resolved_by, created_at "
            "FROM pending_actions WHERE tenant_id = %s"
        )
        params: list[Any] = [tenant]
        if run_id is not None:
            sql += " AND run_id = %s"
            params.append(run_id)
        if status is not None:
            sql += " AND status = %s"
            params.append(status)
        sql += " ORDER BY created_at DESC"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [self._row(r) for r in rows]

    def _save(self, row: dict[str, Any]) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pending_actions
                    (tenant_id, pending_id, run_id, kind, payload, status,
                     resolution, expires_at, resolved_at, resolved_by, created_at)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, pending_id) DO UPDATE SET
                    run_id = EXCLUDED.run_id,
                    kind = EXCLUDED.kind,
                    payload = EXCLUDED.payload,
                    status = EXCLUDED.status,
                    resolution = EXCLUDED.resolution,
                    expires_at = EXCLUDED.expires_at,
                    resolved_at = EXCLUDED.resolved_at,
                    resolved_by = EXCLUDED.resolved_by
                """,
                (
                    str(row["tenant_id"]),
                    str(row["pending_id"]),
                    str(row["run_id"]),
                    str(row["kind"]),
                    json.dumps(row.get("payload") or {}),
                    str(row["status"]),
                    row.get("resolution"),
                    row.get("expires_at"),
                    row.get("resolved_at"),
                    row.get("resolved_by"),
                    row.get("created_at") or _now(),
                ),
            )
            conn.commit()


# --------------------------------------------------------------------------- #
# Factory                                                                      #
# --------------------------------------------------------------------------- #
_STORE: Any = None


def get_pending_store() -> PendingActionStore:
    """Process-wide store: PG when a DSN is configured, else in-memory.

    Mirrors :func:`forgeflow.outcomes.store.get_outcome_store`: the default is the
    in-memory store so every offline path works with no database; the postgres
    profile (``POSTGRES_SYNC_URL`` / ``POSTGRES_DSN`` set) gets the real table.
    """
    global _STORE
    if _STORE is not None:
        return _STORE
    dsn = os.environ.get("POSTGRES_SYNC_URL") or os.environ.get("POSTGRES_DSN")
    if dsn:
        _STORE = PostgresPendingActionStore(dsn)
    else:
        _STORE = InMemoryPendingActionStore()
    return _STORE


def set_pending_store(store: PendingActionStore) -> None:
    """Test helper — pin an explicit store (usually the in-memory one)."""
    global _STORE
    _STORE = store


def reset_pending_store() -> None:
    """Test helper — drop the cached store (next call re-resolves it).

    Simulating a process restart: dropping the cache and re-calling
    :func:`get_pending_store` re-reads the DB (persistence) or starts from an
    empty dict (in-memory ⇒ the record is honestly lost).
    """
    global _STORE
    _STORE = None
