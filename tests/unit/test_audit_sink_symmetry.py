"""Sink-symmetry guard — the writer and the readers resolve the *same* pool.

``middleware/audit.py`` promises "/audit/search and /audit/export are identical
regardless of backend". That promise was false for a hand-assembled app with no
lifespan: the *writer* fell back to ``middleware.audit.available_audit_pool``
(which can return ``database._pool``) while the *readers* only looked at
``request.app.state.pool`` (``None``) — two divergent sinks. The fix routes both
sides through the one resolution point.

These tests pin the symmetry on a **stub pool** (never a real database) so they
are deterministic and memory-profile-safe:

* ``postgres`` backend + an initialised ``database._pool`` ⇒ writer and reader
  resolve to the **identical object** (identity assertion);
* ``memory`` backend ⇒ both resolve to ``None`` ⇒ both use the ring buffer;
* ``postgres`` backend but ``database._pool is None`` ⇒ both ``None``;
* a malformed request / a raising settings resolver never makes ``_soft_pool``
  raise — it degrades to ``None``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import forgeflow.database as _db
from forgeflow.api.routers.audit import _soft_pool
from forgeflow.config import get_settings
from forgeflow.middleware import audit as audit_mw
from forgeflow.middleware.audit import (
    _available_pool,
    available_audit_pool,
    write_audit_entry,
)


class _StubConn:
    def __init__(self, sink: list) -> None:
        self._sink = sink

    async def execute(self, sql: str, *args: Any) -> None:
        self._sink.append((sql, args))


class _StubAcquire:
    def __init__(self, conn: _StubConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _StubConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _StubPool:
    """Minimal asyncpg-pool double — records that a write went through it."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, tuple]] = []

    def acquire(self) -> _StubAcquire:
        return _StubAcquire(_StubConn(self.writes))


def _request(*, pool: Any = None) -> SimpleNamespace:
    """A request double whose ``app.state.pool`` is ``pool`` (default ``None``)."""
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))


# --------------------------------------------------------------------------- #
# a. The core assertion: one pool object for writer and reader                 #
# --------------------------------------------------------------------------- #
async def test_postgres_resolves_one_pool_for_writer_and_reader(monkeypatch):
    monkeypatch.setattr(get_settings(), "storage_backend", "postgres")
    stub = _StubPool()
    monkeypatch.setattr(_db, "_pool", stub)

    writer_pool = available_audit_pool()  # what ``write_audit_entry`` falls back to
    reader_pool = await _soft_pool(_request(pool=None))  # ``/audit/search`` path

    assert writer_pool is reader_pool is stub

    # And the writer *really* used that object: the INSERT lands on the stub.
    await write_audit_entry(
        {"action": "GET", "resource": "/skills", "outcome": "allowed", "metadata": {}},
        pool=None,
    )
    assert stub.writes and "INSERT INTO audit_log" in stub.writes[-1][0]


# --------------------------------------------------------------------------- #
# b. memory profile ⇒ both None ⇒ both ring (transparency)                     #
# --------------------------------------------------------------------------- #
async def test_memory_profile_resolves_to_ring_on_both_paths(
    force_memory_backend, monkeypatch
):
    # Even with a pool object sitting in ``database._pool``, the memory profile
    # must never resolve to it — producer and consumer both fall to the ring.
    monkeypatch.setattr(_db, "_pool", _StubPool())

    assert available_audit_pool() is None
    assert await _soft_pool(_request(pool=None)) is None


# --------------------------------------------------------------------------- #
# c. postgres but no ready pool ⇒ both None ⇒ ring                             #
# --------------------------------------------------------------------------- #
async def test_postgres_without_a_ready_pool_resolves_to_ring(monkeypatch):
    monkeypatch.setattr(get_settings(), "storage_backend", "postgres")
    monkeypatch.setattr(_db, "_pool", None)

    assert available_audit_pool() is None
    assert await _soft_pool(_request(pool=None)) is None


# --------------------------------------------------------------------------- #
# d. _soft_pool never raises on a malformed state                              #
# --------------------------------------------------------------------------- #
async def test_soft_pool_never_raises_when_state_is_malformed(
    force_memory_backend, monkeypatch
):
    # A request lacking ``app`` / ``state`` entirely must not raise.
    assert await _soft_pool(SimpleNamespace()) is None
    assert await _soft_pool(SimpleNamespace(app=SimpleNamespace())) is None

    # A resolver that raises must still degrade to ``None``, never propagate.
    import forgeflow.config as cfg

    def _boom() -> None:
        raise RuntimeError("settings exploded")

    monkeypatch.setattr(cfg, "get_settings", _boom)
    assert available_audit_pool() is None
    assert await _soft_pool(_request(pool=None)) is None


# --------------------------------------------------------------------------- #
# e. The backwards-compatible alias is intact                                  #
# --------------------------------------------------------------------------- #
def test_available_pool_alias_is_intact():
    assert _available_pool is available_audit_pool
    assert audit_mw._available_pool is audit_mw.available_audit_pool
