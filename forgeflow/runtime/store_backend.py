"""FF-G30 — the ONE backend decision for the runtime's paired stores.

The run checkpoint store and the run lease store are two halves of
one fencing protocol: a checkpoint write validates (and can lose)
the lease. They used to decide their backends independently — the
checkpoint store took Postgres whenever a sync DSN was merely
*present* in the environment, the lease store additionally
required the postgres *profile* — so a deployment with a DSN
configured but ``storage_backend=memory`` wrote its first
checkpoint into Postgres, found no ``run_leases`` row there, and
the run died ``lease_lost``.

The rule, shared by both stores through
:func:`select_store_backend`:

* ``storage_backend`` is authoritative;
* ``memory`` → both stores are in-memory, even when a DSN is set;
* ``postgres`` → both stores are Postgres;
* ``postgres`` without a DSN is a fatal misconfiguration, never a
  silent split: the checkpoint store raises
  :class:`StoreBackendConfigurationError` at resolution (its
  message names the missing configuration and never contains a
  URL or a secret value), and the lease store fails closed with
  its own configuration error outside single-process dev mode.
"""

from __future__ import annotations

import os

__all__ = [
    "StoreBackendConfigurationError",
    "postgres_sync_dsn",
    "require_postgres_dsn",
    "select_store_backend",
]


class StoreBackendConfigurationError(RuntimeError):
    """The storage profile and the configured DSNs disagree fatally."""


def postgres_sync_dsn() -> str | None:
    """The configured synchronous Postgres DSN, if any (value never logged)."""
    return os.getenv("POSTGRES_SYNC_URL") or os.getenv("POSTGRES_DSN") or None


def _storage_profile() -> str:
    try:
        from forgeflow.config import get_settings

        return str(get_settings().storage_backend).lower()
    except Exception:  # settings unavailable → the offline profile (fail safe)
        return "memory"


def select_store_backend() -> str:
    """Resolve ``"postgres"`` or ``"memory"`` for BOTH paired stores."""
    return "postgres" if _storage_profile() == "postgres" else "memory"


def require_postgres_dsn() -> str:
    """The DSN a postgres-profile store must dial, or a clear error.

    Raised when the profile says postgres but no DSN is configured:
    building a Postgres store on an empty DSN would fail later and
    obscurely, so the misconfiguration is named here instead. The
    message never contains a URL or a secret value.
    """
    dsn = postgres_sync_dsn()
    if dsn is None:
        raise StoreBackendConfigurationError(
            "storage_backend is 'postgres' but no Postgres DSN is "
            "configured: set POSTGRES_SYNC_URL (or POSTGRES_DSN) for "
            "the checkpoint and lease stores, or switch "
            "storage_backend to 'memory'."
        )
    return dsn
