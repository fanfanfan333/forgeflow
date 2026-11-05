"""Processed-event registry — replay protection for the events layer.

A signed task envelope (see :mod:`forgeflow.events.dispatcher`) carries
a unique ``event_id`` and an ``expires_at``, both covered by the
HMAC signature.  Signatures stop *forgery*; they do not stop *replay*:
a captured, perfectly valid envelope could be re-delivered verbatim
and would pass verification again.  This registry is the other half:
the dispatcher *claims* an event id here before doing any dispatch
work, and a second claim of the same id is a duplicate — rejected
before a run is created.

Claims are persistent (Postgres table ``processed_event_ids``,
migration 045) so protection survives restarts.  The dedupe keyspace
is deliberately GLOBAL, not tenant-scoped, and the table carries no
RLS: an id replayed under a *different* declared tenant must still be
caught, and a row contains nothing but the id itself plus timestamps
(the tenant column is audit metadata only, never used for scoping).

A claim is retained until the event's own ``expires_at`` plus a grace
window — after that, a replayed envelope is rejected by the expiry
check anyway, so the record may lapse and the id may be claimed
again.  The in-memory backend mirrors the same semantics for the
explicit single-process dev/test mode; backend selection mirrors
:mod:`forgeflow.runtime.run_lease` (Postgres when configured,
in-memory only under ``FORGEFLOW_SINGLE_PROCESS_DEV=1``, otherwise a
fatal configuration error — a process-local dedupe registry in a
multi-process deployment would be exactly as broken as a
process-local lease store).

Unsigned dev-mode events have no signed id to claim; the dispatcher
falls back to the transport's ``source_event_id`` as the claim key
with a fixed retention window.  That only dedupes redeliveries of the
same transport message — it cannot stop a re-published copy under a
fresh transport id — which is one more reason unsigned acceptance is
dev-only.

Fork discipline mirrors the lease store: the in-memory registry
records the pid that created it and refuses every claim from any
other process (an inherited copy would happily accept an id the
parent already claimed, reopening the replay window), and the
registry cache is invalidated in a forked child via
``os.register_at_fork``.  Like every process-local store it is
single-machine, single-resource-root development/test
infrastructure only; non-dev/test environments require the Postgres
backend (enforced by the same gate as the lease store).
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

#: How long a claim on an unsigned (dev-mode) event's transport id is
#: retained.  Signed events retain until their own expiry + grace.
UNSIGNED_EVENT_RETENTION = timedelta(hours=24)

#: Grace added to a signed event's ``expires_at`` for claim retention:
#: covers clock skew between producer, consumer and store, so a claim
#: never lapses while the envelope would still verify.
CLAIM_EXPIRY_GRACE = timedelta(hours=1)


class EventDedupeConfigurationError(RuntimeError):
    """No safe processed-event backend for this configuration.

    Outside the explicit single-process dev/test mode, the in-memory
    registry cannot dedupe across processes (a replay delivered to
    another worker would look brand new), so resolution fails closed
    instead of silently downgrading.
    """


class InMemoryProcessedEventStore:
    """Process-local claim registry (single-process dev/test only).

    ``now`` is injectable so tests control retention expiry
    deterministically.  All operations are atomic under one lock.

    Fork discipline: the registry records the pid that created it
    and every claim re-verifies the caller is that same process (see
    :meth:`_assert_owning_process`).  A forked child inherits this
    object by copy; its claims dict is a divergent replica that
    would accept ids the parent already claimed, so claims from any
    other process refuse instead.
    """

    durability = "ephemeral"
    backend = "memory"

    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._claims: dict[str, datetime] = {}
        self._created_pid = os.getpid()

    def _assert_owning_process(self) -> None:
        """Refuse to serve from a registry inherited across ``fork()``.

        Mirrors the run-lease store's check (see
        :meth:`forgeflow.runtime.run_lease.InMemoryRunLeaseStore._assert_owning_process`):
        the calling pid must be the creating pid, and any recorded
        single-process lock confirmation must belong to the calling
        pid rather than to another process.  A mismatch raises
        :class:`EventDedupeConfigurationError` — the dispatcher's
        claim path already fails closed on registry errors.
        """
        from forgeflow.runtime.run_lease import single_process_lock_holder_pid

        pid = os.getpid()
        if pid != self._created_pid:
            raise EventDedupeConfigurationError(
                "in-memory processed-event registry used from a "
                f"different process than the one that created it "
                f"(created by pid {self._created_pid}, called from "
                f"pid {pid}): a forked child inherits a divergent "
                "private copy of the claim registry and must "
                "re-resolve its own backend instead of serving the "
                "inherited one"
            )
        holder = single_process_lock_holder_pid()
        if holder is not None and holder != pid:
            raise EventDedupeConfigurationError(
                "in-memory processed-event registry used in a "
                "process that does not hold the deployment's "
                f"single-process lock (lock confirmation belongs to "
                f"pid {holder}, called from pid {pid}): re-resolve "
                "the registry in this process instead of operating "
                "on inherited state"
            )

    def claim(
        self, event_id: str, *, retention_until: datetime, tenant_id: str | None = None
    ) -> bool:
        """Claim ``event_id``.  True when newly claimed (or the prior
        claim's retention had lapsed); False when it is a live
        duplicate."""
        self._assert_owning_process()
        if not event_id:
            return False
        now = self._now()
        with self._lock:
            existing = self._claims.get(event_id)
            if existing is not None and existing > now:
                return False
            self._claims[event_id] = retention_until
            return True


class PostgresProcessedEventStore:
    """Durable claim registry (migration 045).  A claim is ONE atomic
    conditional insert: it succeeds only when the id is absent or its
    previous claim's retention has lapsed."""

    durability = "durable"
    backend = "postgres"

    def __init__(self, dsn: str) -> None:
        from forgeflow.runtime.checkpoints import _normalise_dsn

        self._dsn = _normalise_dsn(dsn)

    def claim(
        self, event_id: str, *, retention_until: datetime, tenant_id: str | None = None
    ) -> bool:
        if not event_id:
            return False
        import psycopg

        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO processed_event_ids (event_id, tenant_id, expires_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (event_id) DO UPDATE
                    SET processed_at = now(), expires_at = EXCLUDED.expires_at
                    WHERE processed_event_ids.expires_at <= now()
                RETURNING event_id
                """,
                (event_id, tenant_id, retention_until),
            )
            row = cur.fetchone()
            conn.commit()
        return row is not None


# --------------------------------------------------------------------------- #
# Registry (mirrors the run-lease store registry)
# --------------------------------------------------------------------------- #
_STORE: Any = None

#: One-per-process guard for the process-local-store warning (see
#: the run-lease registry); reset in the at-fork child hook.
_PROCESS_LOCAL_NOTICE_EMITTED = False


def _log_process_local_store_notice() -> None:
    """Warn (once per process) that the in-memory registry is live:
    it dedupes only within this process, on this machine."""
    global _PROCESS_LOCAL_NOTICE_EMITTED
    if _PROCESS_LOCAL_NOTICE_EMITTED:
        return
    _PROCESS_LOCAL_NOTICE_EMITTED = True
    logger.warning(
        "process-local processed-event registry ACTIVE: replay "
        "protection lives in this process's memory, coordinated only "
        "by a lock file under one resource root on one machine. This "
        "is single-machine development/test mode only — every "
        "non-dev/test environment requires the Postgres backend."
    )


def get_processed_event_store() -> Any:
    global _STORE
    if _STORE is not None:
        return _STORE
    from forgeflow.runtime.run_lease import _postgres_profile, process_local_stores_allowed

    sync_url = os.getenv("POSTGRES_SYNC_URL") or os.getenv("POSTGRES_DSN")
    if sync_url and _postgres_profile():
        _STORE = PostgresProcessedEventStore(sync_url)
    elif process_local_stores_allowed():
        _STORE = InMemoryProcessedEventStore()
        _log_process_local_store_notice()
    else:
        raise EventDedupeConfigurationError(
            "event replay protection requires the Postgres backend "
            "outside single-process dev/test mode: no Postgres store "
            "is available for processed event ids, and an in-memory "
            "registry cannot dedupe replays delivered to another "
            "process. Configure the Postgres backend, or set "
            "FORGEFLOW_SINGLE_PROCESS_DEV=1 (with exactly one worker) "
            "for single-process development — the in-memory registry "
            "is then handed out only to the one live process holding "
            "the deployment's single-process lock."
        )
    return _STORE


def set_processed_event_store(store: Any) -> None:
    global _STORE
    _STORE = store


def reset_processed_event_store() -> None:
    global _STORE
    _STORE = None


def _reset_registry_after_fork() -> None:
    """``os.register_at_fork`` child hook: drop the inherited cache.

    The forked child must not be handed the parent's registry object
    (its per-claim pid check would refuse every call anyway); with
    the cache invalidated, a child that re-resolves goes through the
    full backend-selection gate again — the Postgres backend, or the
    in-memory backend only once this process itself is confirmed as
    the deployment's single live process.
    """
    global _STORE, _PROCESS_LOCAL_NOTICE_EMITTED
    _STORE = None
    _PROCESS_LOCAL_NOTICE_EMITTED = False


if hasattr(os, "register_at_fork"):  # POSIX; unavailable on some platforms
    os.register_at_fork(after_in_child=_reset_registry_after_fork)
