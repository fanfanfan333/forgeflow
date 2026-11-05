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
    """

    durability = "ephemeral"
    backend = "memory"

    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._claims: dict[str, datetime] = {}

    def claim(
        self, event_id: str, *, retention_until: datetime, tenant_id: str | None = None
    ) -> bool:
        """Claim ``event_id``.  True when newly claimed (or the prior
        claim's retention had lapsed); False when it is a live
        duplicate."""
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


def get_processed_event_store() -> Any:
    global _STORE
    if _STORE is not None:
        return _STORE
    from forgeflow.runtime.run_lease import _postgres_profile, single_process_dev_mode

    sync_url = os.getenv("POSTGRES_SYNC_URL") or os.getenv("POSTGRES_DSN")
    if sync_url and _postgres_profile():
        _STORE = PostgresProcessedEventStore(sync_url)
    elif single_process_dev_mode():
        _STORE = InMemoryProcessedEventStore()
    else:
        raise EventDedupeConfigurationError(
            "event replay protection requires the Postgres backend "
            "outside single-process dev/test mode: no Postgres store "
            "is available for processed event ids, and an in-memory "
            "registry cannot dedupe replays delivered to another "
            "process. Configure the Postgres backend, or set "
            "FORGEFLOW_SINGLE_PROCESS_DEV=1 (with exactly one worker) "
            "for single-process development."
        )
    return _STORE


def set_processed_event_store(store: Any) -> None:
    global _STORE
    _STORE = store


def reset_processed_event_store() -> None:
    global _STORE
    _STORE = None
