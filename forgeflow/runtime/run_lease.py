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
profile must never dial out.  The InMemory backend is process-local by
definition, so it is allowed ONLY in the explicit single-process
dev/test mode (``Settings.environment() == "dev"`` — the INC12 A3
``app_env`` signal): two processes sharing an InMemory store do not
share anything, and each would mint fencing token 1 for the same run.
Outside dev/test mode, a missing Postgres lease backend is a fatal
configuration error (:class:`RunLeaseConfigurationError`), raised at
store resolution and again at application startup — never a silent
downgrade.

Leasing is unconditional: a run whose context carries no tenant
(legitimate internal/system tasks) leases under the fixed
:data:`SYSTEM_LEASE_TENANT`, so there is no tenant-less bypass.

Fencing depth — where the token is checked, honestly stated:

* **Atomically fenced at commit.** Writes whose run-scoped commit goes
  through an in-repo store validate the token at the receiving end, at
  commit time: ``ArtifactReviewStore.create_pending_version`` (the
  commit point for ``document.edit`` / ``textfile.edit`` /
  ``sheet.edit``) calls :func:`assert_fenced_commit` before persisting
  — inside the in-memory store's lock (its critical section), and for
  the Postgres store additionally as a conditional ``run_leases``
  check inside the same transaction as the version insert.  A failed
  check raises :class:`LeaseFencedError` and the write is NOT
  persisted.  The loop's own checkpoint saves are likewise guarded at
  the loop seams (pre-write save and round save) before persisting.
  ``DocArtifactStore.put`` is deliberately NOT fenced: it is a
  content-addressed blob write with no tenant/run scope, unreferenced
  until a fenced version row points at it.
* **Post-checked only.** External side effects cannot be fenced
  atomically — MCP tool calls, ``code.execute`` (subprocess),
  ``code.commit`` (git subprocess + file apply), ``pdf.generate``
  (blob-only) and every other write-class call: the executor
  re-validates the lease after the handler completes, and if the lease
  was lost mid-handler the invocation becomes ``outcome_unknown``
  (never success/confirmed) and the run's lease session is marked lost
  so the loop terminates as ``lease_lost``.  ``outcome_unknown`` there
  means exactly what it says: the external side effect may or may not
  have happened; the platform treats it as unknown and stops instead
  of confirming it.
* The pre-handler validation in ``ToolExecutor`` remains the first
  gate for every lease-stamped write-class call (a call that is
  already stale when it arrives is refused did-not-execute); the two
  layers above close the takeover-mid-handler race it cannot see.

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

#: Fixed lease tenant for runs whose context carries no tenant
#: (internal/system tasks).  Leasing is unconditional — there is no
#: tenant-less skip — so such runs key their lease under this reserved
#: tenant instead.  The value is deliberately not a plausible real
#: tenant id; collisions are harmless anyway because the lease key is
#: (tenant, run_id) and run ids are unique.
SYSTEM_LEASE_TENANT = "__forgeflow_system__"


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


class RunLeaseConfigurationError(RuntimeError):
    """No safe lease backend for this deployment configuration.

    Raised when the configuration is production-like (non-dev
    environment) but no Postgres lease backend is available: falling
    back to the process-local store would silently break cross-process
    exclusion (every process mints fencing token 1 for the same run),
    so resolution and startup fail closed instead.
    """


class LeaseFencedError(RuntimeError):
    """A store commit was refused by commit-time lease fencing.

    Raised by :func:`assert_fenced_commit` at the side-effect-receiving
    end when the executing run no longer holds its lease (taken over
    after expiry, or the lease store errored — fail closed).  The
    write carrying this error was NOT persisted.
    """


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


def single_process_dev_mode() -> bool:
    """True only in the explicit single-process dev/test mode.

    The signal is the codebase's existing deployment gate,
    ``Settings.environment()`` (INC12 A3, from ``app_env``): ``"dev"``
    (the field default, and the test profile) is the single-process
    development mode; ``"staging"`` / ``"prod"`` — and any unrecognised
    label, which ``environment()`` normalises fail-closed to
    ``"prod"`` — are production-like.  The in-memory lease backend is
    process-local, so it is only sound where a single process exists
    by construction: dev/test.  Settings being unreadable is NOT dev
    mode (fail closed).
    """
    try:
        from forgeflow.config import get_settings

        return get_settings().environment() == "dev"
    except Exception:  # settings unavailable → not dev (fail closed)
        return False


def get_run_lease_store() -> Any:
    global _STORE
    if _STORE is not None:
        return _STORE
    sync_url = os.getenv("POSTGRES_SYNC_URL") or os.getenv("POSTGRES_DSN")
    if sync_url and _postgres_profile():
        _STORE = PostgresRunLeaseStore(sync_url)
    elif single_process_dev_mode():
        _STORE = InMemoryRunLeaseStore()
    else:
        # Fail closed: outside single-process dev/test mode the
        # in-memory store cannot exclude other processes (each would
        # mint fencing token 1 for the same run), so a missing
        # Postgres backend is a fatal misconfiguration — at resolution
        # here and at application startup — never a silent downgrade.
        raise RunLeaseConfigurationError(
            "run leases require the Postgres backend outside "
            "single-process dev/test mode: no Postgres lease store is "
            "available (STORAGE_BACKEND is not 'postgres' or no "
            "POSTGRES_SYNC_URL / POSTGRES_DSN is configured), and the "
            "in-memory lease store cannot exclude other processes. "
            "Configure the Postgres backend, or set APP_ENV=dev for "
            "single-process development."
        )
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


def assert_fenced_commit(tenant_id: str, run_id: str | None) -> None:
    """Commit-time fencing check for in-repo stores.

    Called by a store at the moment it is about to persist a write that
    belongs to a run (``run_id``).  When the current execution context
    holds that run's lease session, the lease is validated against the
    lease store *now* — at the receiving end, not before the handler
    started — so a takeover that landed mid-handler is observed and
    the commit is refused with :class:`LeaseFencedError` (the caller
    must not persist).  The session is also marked lost, so the loop
    stops instead of attempting further writes.

    Contexts with no lease session, writes for other runs, and writes
    with no run scope pass through unfenced: they are not one run's
    writes racing a takeover (direct store callers, reviewer actions,
    read paths).
    """
    if not run_id:
        return
    session = current_run_lease.get()
    if session is None:
        return
    lease = session.lease
    if lease.tenant_id != tenant_id or lease.run_id != run_id:
        return
    if session.lost or not session.still_valid():
        session.mark_lost()
        raise LeaseFencedError(
            f"run {run_id} lease fenced at commit: owner {lease.owner} "
            f"(fencing token {lease.fencing_token}) no longer holds the "
            "lease — it expired and was taken over, or the lease store "
            "failed; the write was NOT persisted"
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
