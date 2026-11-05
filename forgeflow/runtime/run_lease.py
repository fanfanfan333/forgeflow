"""Run session leases — one executor per run, with fencing tokens.

Borrowed ideas (not code): OpenHands' session lease (a run/session is
owned by exactly one worker at a time) and the fencing-token pattern
(Kleppmann; LangGraph-style single-writer runs): every acquisition of a
run's lease bumps a monotonically increasing ``fencing_token``, and
every write performed on behalf of the run must present the current
token.  A worker whose lease was taken over after expiry keeps its old
token, so its late writes are *definitively* refused (did-not-execute)
instead of silently racing the new owner.

Two store backends mirror :mod:`forgeflow.runtime.checkpoints`:

* :class:`PostgresRunLeaseStore` — durable; acquisition is ONE atomic
  conditional ``INSERT ... ON CONFLICT ... WHERE expires_at <= now()``
  statement (no read-then-write race).  Renew / validate / release are
  likewise single conditional statements keyed on (owner, fencing
  token).  Tenant scoping uses the same per-connection
  ``set_config('app.tenant_id', ...)`` pattern as the checkpoint store,
  backed by the ``run_leases`` table (migration 044, RLS-enforced).
* :class:`InMemoryRunLeaseStore` — ephemeral; same semantics under a
  lock, with an injectable clock so tests control expiry
  deterministically.

The module-level registry (:func:`get_run_lease_store` /
:func:`set_run_lease_store` / :func:`reset_run_lease_store`) picks the
backend the way the codebase picks its other stores: Postgres when a
sync DSN is configured (``POSTGRES_SYNC_URL`` / ``POSTGRES_DSN``, the
checkpoint rule) *and* the storage profile is postgres (the repository
factory rule).  The lease is consulted on every run — unlike the
checkpoint store, which offline tests pin or avoid — so the memory
profile must never dial out; it gets the InMemory store, and existing
in-process callers keep working unchanged.

The executor-side state rides on :data:`current_run_lease`, a
contextvar in the same style as ``current_budget`` / ``current_recovery``:
:func:`forgeflow.runtime.orchestrator.run_task` installs a
:class:`RunLeaseSession` for the duration of a run, a background task
renews it, and the react loop + :class:`ToolExecutor` consult it to
fence stale writes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import threading
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Configuration (retry_policy-style: module constants + task.context override)
# --------------------------------------------------------------------------- #
DEFAULT_LEASE_TTL_SECONDS = 120.0
"""Default lease TTL.  Renewal runs at TTL/3 (≈40s), so a crashed worker's
run becomes resumable by another worker within ~2 minutes while a healthy
worker never comes close to expiry."""

_LEASE_TTL_MIN_SECONDS = 0.05
_LEASE_TTL_MAX_SECONDS = 3600.0
_LEASE_TTL_CONTEXT_KEY = "run_lease_ttl_seconds"
_LEASE_RENEW_INTERVAL_CONTEXT_KEY = "run_lease_renew_interval_seconds"


def resolve_lease_ttl_seconds(task: Any) -> float:
    """Resolve the lease TTL for a task: ``task.context`` override wins.

    Fail-closed like ``retry_policy``'s overrides: a non-numeric or
    out-of-range value falls back to the default rather than disabling
    or exploding the lease.
    """
    context = getattr(task, "context", None)
    if isinstance(context, dict):
        raw = context.get(_LEASE_TTL_CONTEXT_KEY)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            value = float(raw)
            if _LEASE_TTL_MIN_SECONDS <= value <= _LEASE_TTL_MAX_SECONDS:
                return value
    return DEFAULT_LEASE_TTL_SECONDS


def lease_renew_interval_seconds(ttl_seconds: float) -> float:
    """Background renewal interval: one third of the TTL."""
    return ttl_seconds / 3.0


def resolve_lease_renew_interval_seconds(task: Any, ttl_seconds: float) -> float:
    """Renewal cadence for a task: ``task.context`` override wins.

    Defaults to TTL/3.  An explicit override must be positive and below
    the TTL (a cadence at or above the TTL could let a healthy lease
    lapse between ticks); anything else falls back to the default.
    """
    context = getattr(task, "context", None)
    if isinstance(context, dict):
        raw = context.get(_LEASE_RENEW_INTERVAL_CONTEXT_KEY)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            value = float(raw)
            if 0.0 < value < ttl_seconds:
                return value
    return lease_renew_interval_seconds(ttl_seconds)


# --------------------------------------------------------------------------- #
# Lease record + refusal error
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RunLease:
    """One held lease.  ``fencing_token`` strictly increases on every
    successful acquisition of the same (tenant, run) — a token is never
    reused, so presenting an old token proves staleness."""

    tenant_id: str
    run_id: str
    owner: str
    fencing_token: int
    acquired_at: datetime
    expires_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "run_id": self.run_id,
            "owner": self.owner,
            "fencing_token": self.fencing_token,
            "acquired_at": self.acquired_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }


class RunLeaseHeldError(RuntimeError):
    """Acquisition failed: the run is leased by another worker.

    Carries the current holder so refusal surfaces (run record error,
    API response) can name *who* holds the run and *until when* — the
    same facts an operator needs to decide whether to wait or take over
    after expiry.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        run_id: str,
        owner: str | None,
        expires_at: datetime | None,
    ) -> None:
        self.tenant_id = tenant_id
        self.run_id = run_id
        self.owner = owner
        self.expires_at = expires_at
        holder = owner if owner else "another worker"
        until = expires_at.isoformat() if expires_at is not None else "unknown expiry"
        super().__init__(
            f"run {run_id} is already leased by {holder} until {until}; "
            "refusing concurrent execution/resume"
        )


# --------------------------------------------------------------------------- #
# Owner identity
# --------------------------------------------------------------------------- #
_PROCESS_OWNER: str | None = None


def default_lease_owner() -> str:
    """Per-process worker identity: ``hostname:pid:uuid-prefix``.

    Generated once per process — two processes on one host differ by
    pid/uuid, two runs in one process share the identity (they are the
    same worker; the lease distinguishes them by run, not by owner).
    Tests override the owner per execution instead of relying on this.
    """
    global _PROCESS_OWNER
    if _PROCESS_OWNER is None:
        try:
            host = socket.gethostname() or "unknown-host"
        except OSError:  # pragma: no cover - hostname is best-effort
            host = "unknown-host"
        _PROCESS_OWNER = f"{host}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
    return _PROCESS_OWNER


# --------------------------------------------------------------------------- #
# InMemory store
# --------------------------------------------------------------------------- #
@dataclass
class _LeaseRow:
    owner: str
    fencing_token: int
    acquired_at: datetime
    expires_at: datetime
    # Release is a tombstone, not a delete: the row (and its token)
    # survives so the NEXT acquisition still bumps the token — tokens
    # strictly increase on every acquisition of the run, even across a
    # clean release, so a token is never reused while a stale holder
    # might still present it.
    released: bool = False


class InMemoryRunLeaseStore:
    """Process-local lease store (``durability='ephemeral'``).

    ``now`` is injectable so unit tests advance time deterministically
    instead of sleeping.  All operations are atomic under one lock.
    """

    durability = "ephemeral"
    backend = "memory"

    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._rows: dict[tuple[str, str], _LeaseRow] = {}

    def _snapshot(self, tenant_id: str, run_id: str, row: _LeaseRow) -> RunLease:
        return RunLease(
            tenant_id=tenant_id,
            run_id=run_id,
            owner=row.owner,
            fencing_token=row.fencing_token,
            acquired_at=row.acquired_at,
            expires_at=row.expires_at,
        )

    def acquire(
        self, tenant_id: str, run_id: str, owner: str, ttl_seconds: float
    ) -> RunLease | None:
        if not tenant_id or not run_id or not owner:
            return None
        now = self._now()
        with self._lock:
            row = self._rows.get((tenant_id, run_id))
            if row is not None and not row.released and row.expires_at > now:
                return None  # held and unexpired
            token = (row.fencing_token + 1) if row is not None else 1
            row = _LeaseRow(
                owner=owner,
                fencing_token=token,
                acquired_at=now,
                expires_at=now + timedelta(seconds=ttl_seconds),
            )
            self._rows[(tenant_id, run_id)] = row
            return self._snapshot(tenant_id, run_id, row)

    def peek(self, tenant_id: str, run_id: str) -> RunLease | None:
        with self._lock:
            row = self._rows.get((tenant_id, run_id))
            if row is None:
                return None
            return self._snapshot(tenant_id, run_id, row)

    def renew(self, lease: RunLease, ttl_seconds: float) -> bool:
        now = self._now()
        with self._lock:
            row = self._rows.get((lease.tenant_id, lease.run_id))
            if (
                row is None
                or row.released
                or row.owner != lease.owner
                or row.fencing_token != lease.fencing_token
            ):
                return False
            # Expiry alone does not lose the lease: renewal succeeds even
            # past expires_at as long as nobody took over (token intact).
            row.expires_at = now + timedelta(seconds=ttl_seconds)
            return True

    def validate(self, tenant_id: str, run_id: str, owner: str, fencing_token: int) -> bool:
        now = self._now()
        with self._lock:
            row = self._rows.get((tenant_id, run_id))
            return (
                row is not None
                and not row.released
                and row.owner == owner
                and row.fencing_token == fencing_token
                and row.expires_at > now
            )

    def release(self, lease: RunLease) -> bool:
        with self._lock:
            row = self._rows.get((lease.tenant_id, lease.run_id))
            if (
                row is None
                or row.released
                or row.owner != lease.owner
                or row.fencing_token != lease.fencing_token
            ):
                return False  # never release someone else's lease
            row.released = True  # tombstone: keeps the token monotonic
            return True


# --------------------------------------------------------------------------- #
# Postgres store
# --------------------------------------------------------------------------- #
class PostgresRunLeaseStore:
    """Durable lease store.  Every operation is a single conditional
    statement: acquisition atomically takes the row only when it is
    absent or expired (bumping the fencing token); renew / validate /
    release are conditional on (owner, fencing_token).  Tenant scoping
    matches :class:`PostgresCheckpointStore` (``app.tenant_id`` set per
    connection; migration 044 enforces it with RLS)."""

    durability = "durable"
    backend = "postgres"

    def __init__(self, dsn: str) -> None:
        from forgeflow.runtime.checkpoints import _normalise_dsn

        self._dsn = _normalise_dsn(dsn)

    def _connect(self, tenant_id: str):  # noqa: ANN202 - psycopg connection
        import psycopg
        from psycopg.rows import dict_row

        from forgeflow.tenant_scope import current_tenant

        conn = psycopg.connect(self._dsn, row_factory=dict_row)
        effective_tenant = tenant_id or current_tenant()
        # Transaction-local: SET LOCAL semantics via set_config(..., true)
        # so the setting can never leak across pooled checkouts.
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.tenant_id', %s, true)", (effective_tenant,))
        conn.commit()
        return conn

    def acquire(
        self, tenant_id: str, run_id: str, owner: str, ttl_seconds: float
    ) -> RunLease | None:
        if not tenant_id or not run_id or not owner:
            return None
        conn = self._connect(tenant_id)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO run_leases (tenant_id, run_id, owner, fencing_token, expires_at)
                    VALUES (%s, %s, %s, 1, now() + make_interval(secs => %s))
                    ON CONFLICT (tenant_id, run_id) DO UPDATE
                        SET owner = EXCLUDED.owner,
                            fencing_token = run_leases.fencing_token + 1,
                            acquired_at = now(),
                            renewed_at = now(),
                            expires_at = EXCLUDED.expires_at,
                            released = false
                        WHERE run_leases.expires_at <= now() OR run_leases.released
                    RETURNING owner, fencing_token, acquired_at, expires_at
                    """,
                    (tenant_id, run_id, owner, ttl_seconds),
                )
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()
        if row is None:
            return None  # held and unexpired (or blocked by RLS)
        return RunLease(
            tenant_id=tenant_id,
            run_id=run_id,
            owner=row["owner"],
            fencing_token=int(row["fencing_token"]),
            acquired_at=row["acquired_at"],
            expires_at=row["expires_at"],
        )

    def peek(self, tenant_id: str, run_id: str) -> RunLease | None:
        conn = self._connect(tenant_id)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT owner, fencing_token, acquired_at, expires_at FROM run_leases "
                    "WHERE tenant_id = %s AND run_id = %s",
                    (tenant_id, run_id),
                )
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()
        if row is None:
            return None
        return RunLease(
            tenant_id=tenant_id,
            run_id=run_id,
            owner=row["owner"],
            fencing_token=int(row["fencing_token"]),
            acquired_at=row["acquired_at"],
            expires_at=row["expires_at"],
        )

    def renew(self, lease: RunLease, ttl_seconds: float) -> bool:
        conn = self._connect(lease.tenant_id)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE run_leases
                    SET expires_at = now() + make_interval(secs => %s), renewed_at = now()
                    WHERE tenant_id = %s AND run_id = %s AND owner = %s AND fencing_token = %s
                        AND released = false
                    RETURNING expires_at
                    """,
                    (ttl_seconds, lease.tenant_id, lease.run_id, lease.owner, lease.fencing_token),
                )
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()
        return row is not None

    def validate(self, tenant_id: str, run_id: str, owner: str, fencing_token: int) -> bool:
        conn = self._connect(tenant_id)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 AS ok FROM run_leases WHERE tenant_id = %s AND run_id = %s "
                    "AND owner = %s AND fencing_token = %s AND expires_at > now() "
                    "AND released = false LIMIT 1",
                    (tenant_id, run_id, owner, fencing_token),
                )
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()
        return row is not None

    def release(self, lease: RunLease) -> bool:
        # Tombstone UPDATE, not a DELETE: the row keeps the fencing
        # token so the next acquisition still bumps it (tokens strictly
        # increase on every acquisition of the run, even across a clean
        # release — a token is never reused while a stale holder might
        # still present it).
        conn = self._connect(lease.tenant_id)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE run_leases SET released = true WHERE tenant_id = %s AND run_id = %s "
                    "AND owner = %s AND fencing_token = %s AND released = false",
                    (lease.tenant_id, lease.run_id, lease.owner, lease.fencing_token),
                )
                updated = cur.rowcount
            conn.commit()
        finally:
            conn.close()
        return bool(updated)


# --------------------------------------------------------------------------- #
# Registry (mirrors the checkpoint store registry exactly)
# --------------------------------------------------------------------------- #
_STORE: Any = None


def _postgres_profile() -> bool:
    """True when the configured storage profile is postgres.

    Mirrors ``forgeflow.repositories.factory._backend``: the profile —
    not just the presence of a DSN in the environment — decides whether
    durable stores may dial out.  Settings are read at call time so
    test fixtures that pin ``storage_backend`` take effect.
    """
    try:
        from forgeflow.config import get_settings

        return str(get_settings().storage_backend).lower() == "postgres"
    except Exception:  # settings unavailable → stay offline (fail safe)
        return False


def get_run_lease_store() -> Any:
    global _STORE
    if _STORE is not None:
        return _STORE
    sync_url = os.getenv("POSTGRES_SYNC_URL") or os.getenv("POSTGRES_DSN")
    if sync_url and _postgres_profile():
        _STORE = PostgresRunLeaseStore(sync_url)
    else:
        _STORE = InMemoryRunLeaseStore()
    return _STORE


def set_run_lease_store(store: Any) -> None:
    global _STORE
    _STORE = store


def reset_run_lease_store() -> None:
    global _STORE
    _STORE = None


# --------------------------------------------------------------------------- #
# Executor-side session (contextvar) + background renewal
# --------------------------------------------------------------------------- #
@dataclass
class RunLeaseSession:
    """The lease as seen by one executing run: the acquired lease, the
    store that issued it, and the ``lost`` flag the renewal task flips
    when the lease is taken over (or the store fails — fail-closed)."""

    store: Any
    lease: RunLease
    ttl_seconds: float
    lost: bool = field(default=False)

    @property
    def owner(self) -> str:
        return self.lease.owner

    @property
    def fencing_token(self) -> int:
        return self.lease.fencing_token

    def mark_lost(self) -> None:
        self.lost = True

    def still_valid(self) -> bool:
        """Authoritative check: recorded holder + token + unexpired."""
        if self.lost:
            return False
        try:
            return bool(
                self.store.validate(
                    self.lease.tenant_id,
                    self.lease.run_id,
                    self.lease.owner,
                    self.lease.fencing_token,
                )
            )
        except Exception:  # store error → fail closed
            logger.warning("run lease validate failed; treating lease as lost", exc_info=True)
            self.mark_lost()
            return False


current_run_lease: ContextVar[RunLeaseSession | None] = ContextVar(
    "forgeflow_current_run_lease", default=None
)


def acquire_run_lease(tenant_id: str, run_id: str, owner: str, ttl_seconds: float) -> RunLease:
    """Acquire or raise :class:`RunLeaseHeldError` naming the holder."""
    store = get_run_lease_store()
    lease = store.acquire(tenant_id, run_id, owner, ttl_seconds)
    if lease is None:
        holder = None
        with contextlib.suppress(Exception):
            holder = store.peek(tenant_id, run_id)
        raise RunLeaseHeldError(
            tenant_id=tenant_id,
            run_id=run_id,
            owner=holder.owner if holder is not None else None,
            expires_at=holder.expires_at if holder is not None else None,
        )
    return lease


async def renew_lease_loop(session: RunLeaseSession, interval_seconds: float) -> None:
    """Renew the session lease until cancelled; flip ``lost`` on failure.

    A failed renew means the fencing token no longer matches (another
    worker took over after expiry) or the store errored — both are
    fail-closed: the run must stop writing and exit safely rather than
    race an owner it can no longer prove it is.
    """
    store = session.store
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            renewed = await asyncio.to_thread(store.renew, session.lease, session.ttl_seconds)
        except Exception:
            logger.warning(
                "run lease renewal errored for run %s; marking lease lost",
                session.lease.run_id,
                exc_info=True,
            )
            renewed = False
        if not renewed:
            session.mark_lost()
            logger.warning(
                "run lease lost for run %s (owner %s, token %s); execution must stop",
                session.lease.run_id,
                session.lease.owner,
                session.lease.fencing_token,
            )
            return
