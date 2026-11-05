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
dev/test mode: the operator must set ``FORGEFLOW_SINGLE_PROCESS_DEV=1``
(see :func:`single_process_dev_mode`).  The deployment environment
label (``app_env``, whose default is ``dev``) must NEVER by itself
enable it — an inference from a default is how a production deployment
ends up "in dev mode" without anyone deciding that.  Two processes
sharing an InMemory store do not share anything, and each would mint
fencing token 1 for the same run; a multi-worker configuration
(``WEB_CONCURRENCY`` ≠ 1, the signal ``forgeflow.bootstrap`` uses)
refuses the dev mode even when the flag is set.  And the flag is only
a *claim* about the launch configuration — a supervisor can spawn
several processes without setting any observable signal — so the
in-memory backend is handed out only to a process that has POSITIVELY
confirmed it is the deployment's only live process, by holding an
OS-level exclusive lock for its lifetime (see
:func:`confirm_single_process`); a second live process fails closed.
Outside the explicit, lock-confirmed dev mode, a missing Postgres
lease backend is a fatal configuration error
(:class:`RunLeaseConfigurationError`), raised at store resolution and
again at application startup — never a silent downgrade.

Leasing is unconditional: a run whose context carries no tenant
(legitimate internal/system tasks) leases under the fixed
:data:`SYSTEM_LEASE_TENANT`, so there is no tenant-less bypass.

Fencing depth — where the token is checked, honestly stated:

* **Atomically fenced at commit.** Writes whose run-scoped commit goes
  through an in-repo store validate the token at the receiving end, at
  commit time, *inside the same critical section / transaction as the
  write itself*, so a takeover cannot slip between the check and the
  write: ``ArtifactReviewStore.create_pending_version`` (the commit
  point for ``document.edit`` / ``textfile.edit`` / ``sheet.edit``)
  validates under the in-memory lease store's own lock — the same lock
  lease acquisition contends on, via :meth:`InMemoryRunLeaseStore.
  fenced_commit` — and for the Postgres store it locks the
  ``run_leases`` row ``SELECT ... FOR UPDATE`` inside the same
  transaction as the version + review inserts, so a concurrent
  takeover blocks until the commit lands (and then proceeds against
  the new token).  A failed check raises :class:`LeaseFencedError` and
  the write is NOT persisted; a blob written for a commit that is then
  refused is removed again (no orphaned blobs).  The loop's own
  checkpoint saves (pre-write save and round save) go through the
  checkpoint stores' fenced ``save`` — the same pattern: validation
  inside the save's lock (in-memory) or transaction with the lease
  row locked (Postgres), never a check-then-save at the loop seam.
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
import tempfile
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

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

#: Tenant names that exist only inside the platform: the system lease
#: tenant above and the security-audit workspace id
#: (``forgeflow.middleware.audit`` stamps its own audit rows with it).
#: External producers must never be able to claim them — the events
#: dispatcher rejects any envelope declaring one, signed or not, in
#: any mode: system work is generated internally, never accepted from
#: a bus.  One shared set so every entry point refuses the same names.
RESERVED_TENANT_NAMES: frozenset[str] = frozenset(
    {
        SYSTEM_LEASE_TENANT,
        "__forgeflow_security__",
    }
)


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

    Raised when the deployment is not in the explicit single-process
    dev/test mode (``FORGEFLOW_SINGLE_PROCESS_DEV=1``, one worker)
    but no Postgres lease backend is available — and also when the
    dev opt-in is set yet single-process operation cannot be
    positively confirmed (another live process holds the deployment's
    single-process lock): falling back to the process-local store
    would silently break cross-process exclusion (every process mints
    fencing token 1 for the same run), so resolution and startup fail
    closed instead.
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
class FencedCommitCapable(Protocol):
    """A lease store that can make validate+write atomic in-process.

    Commit-time fencing is only *atomic* when the validation and the
    write it guards cannot be interleaved by a lease takeover.  For
    the in-memory backend that means both must happen while holding
    the lease store's own lock — the same lock ``acquire`` contends
    on — which the store exposes through this protocol as
    :meth:`fenced_commit`.  Stores whose fencing is transactional
    (Postgres: ``SELECT ... FOR UPDATE`` on the lease row inside the
    write's transaction) do not implement it; consumers use
    :func:`lease_fenced_section`, which is a no-op section for them.
    """

    def fenced_commit(self) -> AbstractContextManager[None]:
        """Hold the store lock across a validate+write section."""
        ...


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

    @contextmanager
    def fenced_commit(self) -> Iterator[None]:
        """Hold the store lock across a validate+write section.

        This is the :class:`FencedCommitCapable` affordance: a store
        committing a run-scoped write enters this section, validates
        the lease (``validate`` re-enters the same RLock), and writes
        — all before any pending ``acquire`` (takeover) can proceed,
        because acquisition contends on this very lock.  There is no
        check-then-act window.
        """
        with self._lock:
            yield

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


#: Explicit opt-in for single-process dev/test mode.  Only the exact
#: value ``"1"`` grants it — the mode is never inferred from
#: ``app_env`` / ``Settings.environment()``, whose *default* is
#: ``dev``: an inference from a default would silently put every
#: deployment that forgot to set its environment into "dev mode".
SINGLE_PROCESS_DEV_ENV = "FORGEFLOW_SINGLE_PROCESS_DEV"


def configured_worker_count() -> int:
    """The deployment's configured worker count.

    The signal is the one the deployment actually uses:
    ``WEB_CONCURRENCY`` — ``forgeflow.bootstrap.runtime`` derives its
    uvicorn worker count from exactly this variable (an explicit
    ``--workers`` argument only exists at bootstrap time, in the
    parent process, and is not observable from a worker; a deployment
    that passes it must also keep ``WEB_CONCURRENCY`` consistent).
    An unset value means 1; an unparsable or non-positive value is
    reported as 2 — when the count cannot be proven to be one, the
    caller must treat the deployment as multi-worker (fail closed).

    A reported count of 1 is only the ABSENCE of multi-worker
    evidence — it is not proof of single-process operation (a bare
    ``uvicorn --workers 2`` sets no signal a worker can see).
    Positive confirmation is mechanical, not configurational: see
    :func:`confirm_single_process`.
    """
    raw = os.environ.get("WEB_CONCURRENCY", "1")
    try:
        count = int(raw)
    except (TypeError, ValueError):
        return 2
    return count if count >= 1 else 2


def single_process_dev_mode() -> bool:
    """True only in the explicit single-process dev/test mode.

    Two conditions, both required:

    * the operator explicitly opted in with
      ``FORGEFLOW_SINGLE_PROCESS_DEV=1`` — never inferred from the
      ``app_env`` label (whose default, ``dev``, is exactly the
      unsafe inference this replaces); and
    * the deployment is configured for exactly one worker
      (:func:`configured_worker_count`): a multi-worker deployment
      refuses the mode even when the flag is set, because
      process-local stores cannot exclude the other workers.

    This predicate is the *claim* half of the gate: it reads launch
    configuration only.  Store resolution additionally requires
    :func:`confirm_single_process` (the OS lock) — see
    :func:`process_local_stores_allowed`.
    """
    if os.environ.get(SINGLE_PROCESS_DEV_ENV) != "1":
        return False
    return configured_worker_count() == 1


# --------------------------------------------------------------------------- #
# Single-process confirmation (OS-level exclusive lock)
# --------------------------------------------------------------------------- #
#: Lock file proving THIS process is the only live process serving
#: the process-local stores for this deployment.  It lives in the
#: deployment's resource root — the same root the API instance lock
#: (``forgeflow.runtime.instance_lock``) guards — so every process of
#: one deployment resolves the same file and the lock actually
#: excludes peers.
_SINGLE_PROCESS_LOCK_NAME = ".run-lease-single-process.lock"

#: {"pid": int, "handle": IO} while THIS process holds the lock.
_SINGLE_PROCESS_LOCK: dict[str, Any] | None = None


def _single_process_lock_candidates() -> list[Path]:
    """Lock-file locations, in the order processes try them.

    Primary: the configured resource root
    (``Settings.resource_store_path`` — ``RESOURCE_STORE_ROOT`` →
    ``FORGEFLOW_DATA_DIR``/resources → ``~/.forgeflow/resources``).
    Every process of a deployment resolves the same primary path, so
    contention is real; deployments relying on the process-local
    stores must share a data root — the same caveat the resource
    store itself already carries.  Fallback (only when the primary
    cannot even be attempted): a per-machine temp location, so local
    dev/test on a read-only home still works.
    """
    candidates: list[Path] = []
    try:
        from forgeflow.config import get_settings

        candidates.append(get_settings().resource_store_path() / _SINGLE_PROCESS_LOCK_NAME)
    except Exception:  # settings unavailable → temp fallback only
        pass
    candidates.append(Path(tempfile.gettempdir()) / "forgeflow" / _SINGLE_PROCESS_LOCK_NAME)
    return candidates


def _try_lock_path(path: Path) -> tuple[bool, Any]:
    """One non-blocking exclusive-lock attempt on ``path``.

    Returns ``(attempted, handle)``.  ``attempted`` is False when the
    path could not even be opened (unusable location — the caller may
    fall through to the next candidate).  When the path WAS attempted
    but the lock is held by a live peer, ``attempted`` is True and
    the handle is None: contention is a definitive answer, never a
    reason to try a different file (two processes diverging onto
    different lock files would exclude nobody).  On success the
    handle must be kept for the process lifetime — closing it, or
    process exit, releases the lock.  Mirrors
    ``instance_lock.api_instance_lock``'s flock/msvcrt discipline.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
    except OSError:
        return (False, None)
    try:
        if os.name == "nt":
            import msvcrt

            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"1")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:  # BlockingIOError included: a live peer holds it
        handle.close()
        return (True, None)
    return (True, handle)


def confirm_single_process() -> bool:
    """Positively confirm this process is the ONLY live process that
    may serve the process-local stores; True when confirmed.

    The dev flag plus a one-worker configuration are only *claims*
    about how the deployment was launched — a supervisor spawning N
    processes (``uvicorn --workers 2``) sets no signal the extra
    processes can observe, so claims cannot be the gate.  Confirmation
    is mechanical instead: this process must HOLD an OS-level
    exclusive lock that no second live process can also hold.  The
    lock is taken on first confirmation and held for the process
    lifetime; process exit (or a crash) releases it, so a successor
    process can confirm afterwards.

    Fork-aware: the recorded holder is keyed by pid.  A forked child
    inherits both the record and the descriptor, but descriptor
    possession after a fork proves nothing about which process the
    lock belongs to — so a pid mismatch discards the inherited record
    and makes a FRESH attempt on a fresh descriptor, which fails
    while any live holder (e.g. the parent) still holds the lock.
    """
    global _SINGLE_PROCESS_LOCK
    pid = os.getpid()
    if _SINGLE_PROCESS_LOCK is not None and _SINGLE_PROCESS_LOCK["pid"] == pid:
        return True
    for path in _single_process_lock_candidates():
        attempted, handle = _try_lock_path(path)
        if handle is not None:
            _SINGLE_PROCESS_LOCK = {"pid": pid, "handle": handle}
            return True
        if attempted:
            return False  # a live peer holds this deployment's lock
    return False


def process_local_stores_allowed() -> bool:
    """Whether THIS process may resolve a process-local store backend
    (the in-memory run-lease store, the in-memory processed-event
    registry).

    All three conditions are required: the explicit dev/test opt-in
    (:func:`single_process_dev_mode` — flag set, no multi-worker
    evidence) AND positive single-process confirmation
    (:func:`confirm_single_process` — this process actually holds
    the deployment's exclusive lock).  Absent the lock, resolution
    fails closed: another live process may already be serving the
    same "process-local" state privately.
    """
    return single_process_dev_mode() and confirm_single_process()


def get_run_lease_store() -> Any:
    global _STORE
    if _STORE is not None:
        return _STORE
    sync_url = os.getenv("POSTGRES_SYNC_URL") or os.getenv("POSTGRES_DSN")
    if sync_url and _postgres_profile():
        _STORE = PostgresRunLeaseStore(sync_url)
    elif process_local_stores_allowed():
        _STORE = InMemoryRunLeaseStore()
    else:
        # Fail closed: outside single-process dev/test mode the
        # in-memory store cannot exclude other processes (each would
        # mint fencing token 1 for the same run), so a missing
        # Postgres backend is a fatal misconfiguration — at resolution
        # here and at application startup — never a silent downgrade.
        # The same refusal applies when the dev opt-in IS set but
        # single-process operation cannot be positively confirmed
        # (multi-worker evidence, or another live process already
        # holds the deployment's single-process lock): a configuration
        # *claim* of one worker is not proof of one process.
        raise RunLeaseConfigurationError(
            "run leases require the Postgres backend outside "
            "single-process dev/test mode: no Postgres lease store is "
            "available (STORAGE_BACKEND is not 'postgres' or no "
            "POSTGRES_SYNC_URL / POSTGRES_DSN is configured), and the "
            "in-memory lease store cannot exclude other processes. "
            "Configure the Postgres backend, or set "
            "FORGEFLOW_SINGLE_PROCESS_DEV=1 (with exactly one worker) "
            "for single-process development — and note that even then "
            "the in-memory store is only handed out to the one live "
            "process holding the deployment's single-process lock; a "
            "second live process fails closed here."
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


def current_lease_session_for(tenant_id: str, run_id: str | None) -> RunLeaseSession | None:
    """The current context's lease session, iff it leases (tenant, run).

    ``None`` for contexts with no session and for writes scoped to a
    different run — those are not one run's writes racing a takeover
    (direct store callers, reviewer actions, read paths) and pass
    through unfenced.
    """
    if not run_id:
        return None
    session = current_run_lease.get()
    if session is None:
        return None
    lease = session.lease
    if lease.tenant_id != tenant_id or lease.run_id != run_id:
        return None
    return session


@contextmanager
def lease_fenced_section(session: RunLeaseSession | None) -> Iterator[None]:
    """The critical section a fenced validate+write must run inside.

    For the in-memory lease backend this holds the lease store's own
    lock (via :class:`FencedCommitCapable`) across the caller's
    validate + write, so a lease takeover — which contends on that
    same lock in ``acquire`` — cannot interleave between the two: it
    blocks until the section ends, then proceeds against the state
    the commit left behind.  For the Postgres backend there is no
    process-local lock to hold; the section is a no-op and atomicity
    comes from the caller's transaction locking the lease row
    (``SELECT ... FOR UPDATE``) instead.  A ``None`` session (an
    unfenced write) is likewise a no-op.
    """
    if session is None:
        yield
        return
    guard = getattr(session.store, "fenced_commit", None)
    if guard is None:
        yield
        return
    with guard():
        yield


def assert_fenced_commit(tenant_id: str, run_id: str | None) -> None:
    """Commit-time fencing check for in-repo stores.

    Called by a store at the moment it is about to persist a write that
    belongs to a run (``run_id``) — and, for the in-memory backend,
    inside :func:`lease_fenced_section`, so the check and the write
    are one critical section.  When the current execution context
    holds that run's lease session, the lease is validated against
    the lease store *now* — at the receiving end, not before the
    handler started — so a takeover that landed mid-handler is
    observed and the commit is refused with :class:`LeaseFencedError`
    (the caller must not persist).  The session is also marked lost,
    so the loop stops instead of attempting further writes.

    Contexts with no lease session, writes for other runs, and writes
    with no run scope pass through unfenced.
    """
    session = current_lease_session_for(tenant_id, run_id)
    if session is None:
        return
    lease = session.lease
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
