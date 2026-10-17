"""Pluggable JWT revocation store — shared (Redis) or in-process.

Why this module exists
-----------------------
The original denylist lived entirely inside :mod:`forgeflow.auth.jwt` as a
process-local ``dict + heapq``. That is correct for a single process but makes
"logout on instance A" invisible to instance B. This module extracts the same
behaviour behind a tiny protocol so the decode path can be pointed at a *shared*
backend without touching the JWT public API.

Design (mirrors ``forgeflow.hitl.pending`` — protocol + backends + factory):

* :class:`RevocationStore` — the protocol (``revoke`` / ``is_revoked``);
* :class:`InProcessRevocationStore` — the **verbatim** in-process implementation
  (``_entries`` / ``_expiry`` / ``_sweep_locked`` / ``_lock``); it never raises
  :class:`RevocationUnavailable`, so the offline profile is unchanged;
* :class:`RedisRevocationStore` — ``SETEX`` / ``EXISTS`` over a synchronous
  client, raising :class:`RevocationUnavailable` on any transport error so the
  caller can fail **closed**;
* :func:`get_revocation_store` / :func:`set_revocation_store` /
  :func:`reset_revocation_store` — the process-wide factory.

The default backend is ``memory`` (see ``Settings.auth_revocation_backend``):
this keeps every offline path byte-for-byte identical and never dials a real
Redis during unit tests. ``redis`` / ``auto`` are explicit opt-ins.
"""

from __future__ import annotations

import heapq
import logging
import threading
import time
from typing import Protocol, runtime_checkable

from forgeflow.config import get_settings

logger = logging.getLogger(__name__)

#: Redis key prefix for a revoked jti.
_DEFAULT_PREFIX = "forgeflow:revoked:"

#: Bounded timeouts so a dead Redis can never stall a request.
_SOCKET_TIMEOUT_S = 0.5


class RevocationUnavailable(RuntimeError):
    """The shared revocation store could not answer (transport/timeout/error).

    Raised by a shared backend so :func:`forgeflow.auth.jwt.decode_access_token`
    can apply the fail-closed policy (reject) instead of silently allowing a
    possibly-revoked token. The in-process backend never raises it.
    """

    def __init__(self, jti: str, reason: str) -> None:
        self.jti = jti
        self.reason = reason
        super().__init__(f"revocation store unavailable for jti={jti}: {reason}")


@runtime_checkable
class RevocationStore(Protocol):
    """Minimal denylist interface shared by every backend."""

    def revoke(self, jti: str, exp_epoch: int) -> None:
        """Add ``jti`` to the denylist until its natural ``exp_epoch``."""
        ...

    def is_revoked(self, jti: str) -> bool:
        """Return whether ``jti`` is revoked. Shared backends raise on failure."""
        ...


class InProcessRevocationStore:
    """Process-local jti denylist with TTL eviction.

    Verbatim port of the original ``jwt._RevocationStore`` (``_entries`` /
    ``_expiry`` / ``_sweep_locked`` / ``_lock``) — the internal structure is a
    **regression contract** relied on by ``tests/unit/test_jwt.py``. It never
    raises :class:`RevocationUnavailable` (there is no transport), so the
    offline profile behaves exactly as before.
    """

    def __init__(self) -> None:
        self._entries: dict[str, float] = {}
        self._expiry: list[tuple[float, str]] = []
        self._lock = threading.Lock()

    def revoke(self, jti: str, exp_epoch: int) -> None:
        with self._lock:
            self._sweep_locked()
            expiry = float(exp_epoch)
            if expiry <= time.time() or self._entries.get(jti, 0) >= expiry:
                return
            self._entries[jti] = expiry
            heapq.heappush(self._expiry, (expiry, jti))

    def is_revoked(self, jti: str) -> bool:
        with self._lock:
            self._sweep_locked()
            return jti in self._entries

    def _sweep_locked(self) -> None:
        now = time.time()
        while self._expiry and self._expiry[0][0] <= now:
            expiry, jti = heapq.heappop(self._expiry)
            if self._entries.get(jti) == expiry:
                self._entries.pop(jti, None)


class RedisRevocationStore:
    """Shared jti denylist backed by Redis (``SETEX`` / ``EXISTS``).

    Uses the **synchronous** ``redis`` client (already a transitive dependency of
    ``limits[redis]``; also the optional ``events`` extra). ``redis`` is imported
    lazily so a host without it degrades to :class:`RevocationUnavailable`
    (explicit ``redis`` backend) or to the in-process store (``auto`` backend)
    rather than failing at import time.

    Every transport/timeout/``RedisError`` becomes :class:`RevocationUnavailable`
    so the decode path can fail closed.
    """

    def __init__(self, url: str, prefix: str = _DEFAULT_PREFIX) -> None:
        self._url = url
        self._prefix = prefix
        self._client_obj = None  # lazy — no connection until first use

    def _client(self):  # noqa: ANN202 — redis.Redis, imported lazily
        if self._client_obj is None:
            import redis  # lazy: not a hard dependency of the package

            self._client_obj = redis.Redis.from_url(
                self._url,
                socket_connect_timeout=_SOCKET_TIMEOUT_S,
                socket_timeout=_SOCKET_TIMEOUT_S,
                decode_responses=True,
            )
        return self._client_obj

    def _key(self, jti: str) -> str:
        return f"{self._prefix}{jti}"

    def revoke(self, jti: str, exp_epoch: int) -> None:
        try:
            ttl = max(1, int(exp_epoch) - int(time.time()))
            self._client().setex(self._key(jti), ttl, "")
        except RevocationUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 — any failure is "unavailable"
            raise RevocationUnavailable(jti, f"redis revoke failed: {exc}") from exc

    def is_revoked(self, jti: str) -> bool:
        try:
            return bool(self._client().exists(self._key(jti)))
        except RevocationUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 — any failure is "unavailable"
            raise RevocationUnavailable(jti, f"redis is_revoked failed: {exc}") from exc

    def ping(self, timeout_s: float = _SOCKET_TIMEOUT_S) -> bool:
        """Best-effort reachability probe. Never raises — returns ``False``.

        Used by the ``auto`` backend's lazy probe and by the (non-fatal) startup
        pre-warm. ``timeout_s`` is advisory: the client already bounds every
        socket op at ``_SOCKET_TIMEOUT_S``.
        """
        del timeout_s  # timeouts are pinned on the client; kept for API clarity
        try:
            return bool(self._client().ping())
        except Exception:  # noqa: BLE001 — an unreachable Redis is simply "no"
            return False


# --------------------------------------------------------------------------- #
# Factory                                                                      #
# --------------------------------------------------------------------------- #
_STORE: RevocationStore | None = None
_STORE_RESOLVED = False


def _build_revocation_store() -> RevocationStore:
    """Resolve the configured backend into a concrete store.

    * ``memory`` (default) ⇒ :class:`InProcessRevocationStore` (offline no-op);
    * ``redis`` ⇒ :class:`RedisRevocationStore` (explicit opt-in; raises
      :class:`RevocationUnavailable` at call time when unreachable);
    * ``auto`` ⇒ probe Redis once (bounded) and fall back to the in-process
      store when unreachable — logged so the choice is auditable.
    """
    settings = get_settings()
    backend = (settings.auth_revocation_backend or "memory").strip().lower()

    if backend == "redis":
        logger.info("auth revocation backend=redis")
        return RedisRevocationStore(settings.auth_revocation_redis())

    if backend == "auto":
        probe = RedisRevocationStore(settings.auth_revocation_redis())
        if probe.ping():
            logger.info("auth revocation backend=auto -> redis (reachable)")
            return probe
        logger.info("auth revocation backend=auto -> memory (redis unreachable)")
        return InProcessRevocationStore()

    if backend != "memory":
        logger.warning(
            "unknown AUTH_REVOCATION_BACKEND=%r; falling back to in-process (memory)",
            backend,
        )
    return InProcessRevocationStore()


def get_revocation_store() -> RevocationStore:
    """Process-wide revocation store (lazy, cached).

    The first call resolves + caches the backend. ``auto`` performs its one
    bounded Redis probe here (never on the import/startup critical path beyond
    the single explicit pre-warm the lifespan performs).
    """
    global _STORE, _STORE_RESOLVED
    if not _STORE_RESOLVED:
        _STORE = _build_revocation_store()
        _STORE_RESOLVED = True
    assert _STORE is not None  # noqa: S101 — set immediately above
    return _STORE


def set_revocation_store(store: RevocationStore) -> None:
    """Pin an explicit store (tests / multi-instance simulations)."""
    global _STORE, _STORE_RESOLVED
    _STORE = store
    _STORE_RESOLVED = True


def reset_revocation_store() -> None:
    """Drop the cached store so the next call re-resolves it from config."""
    global _STORE, _STORE_RESOLVED
    _STORE = None
    _STORE_RESOLVED = False


__all__ = [
    "InProcessRevocationStore",
    "RedisRevocationStore",
    "RevocationStore",
    "RevocationUnavailable",
    "get_revocation_store",
    "reset_revocation_store",
    "set_revocation_store",
]
