"""True-lifespan guard for the offline (memory) profile — docs/sop/08 §6.1 / §6.5.

Why this file exists
--------------------
Task #6 fixed ``forgeflow/api/main.py`` so the memory profile boots with **no
reachable PostgreSQL** (``pool=None``; ``init_pool()`` is skipped). But every
pre-existing "offline" test patched ``forgeflow.api.main.init_pool`` into an
``AsyncMock`` — i.e. it replaced the *one line that would crash without a
database* (the exact failure mode named in §6.5: "the test mocks the object
under test"). Those tests can stay green while the real offline boot is broken.

These tests therefore drive the **real** lifespan and deliberately do **not**
patch ``forgeflow.api.main.init_pool``. They point ``POSTGRES_URL`` at
``127.0.0.1:1`` (a port that is guaranteed to refuse) so "no PG" is a
*deterministic* condition rather than an assumption:

  * positive — ``STORAGE_BACKEND=memory`` enters the lifespan, ``GET /health``
    returns 200, and ``app.state.pool is None``;
  * negative control — the same dead PG with ``STORAGE_BACKEND=postgres`` must
    fail at startup (proving the test can actually catch a regression).

Only ``get_mcp_tools`` (an external MCP network collaborator) is patched, so the
suite stays hermetic and fast; ``init_pool`` and ``compile_graph`` run for real.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

# A DSN that can never connect: port 1 is reserved and always refuses.
_DEAD_PG = "127.0.0.1:1"


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Drop the cached settings + module-global pool/checkpointer around a test.

    The sub-app singletons are process-wide (``forgeflow.database._pool``,
    ``forgeflow.graph.checkpointer._checkpointer``) and ``get_settings`` is
    ``lru_cache``d, so each test must start from a clean slate to be
    deterministic under the shared pytest session.
    """
    import forgeflow.database as _db
    import forgeflow.graph.checkpointer as _cp
    from forgeflow.config import get_settings

    get_settings.cache_clear()
    _db._pool = None
    _cp._checkpointer = None
    _cp._cm = None
    yield
    get_settings.cache_clear()
    _db._pool = None
    _cp._checkpointer = None
    _cp._cm = None


def _point_at_dead_postgres(monkeypatch, *, backend: str) -> None:
    """Set a hermetic, PG-less environment for the requested storage backend."""
    monkeypatch.setenv("STORAGE_BACKEND", backend)
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "mock")
    monkeypatch.setenv(
        "POSTGRES_URL", f"postgresql+asyncpg://forgeflow:forgeflow@{_DEAD_PG}/forgeflow"
    )
    monkeypatch.setenv(
        "POSTGRES_SYNC_URL", f"postgresql+psycopg://forgeflow:forgeflow@{_DEAD_PG}/forgeflow"
    )
    monkeypatch.setenv("OTEL_ENVIRONMENT", "development")
    monkeypatch.setenv("EVENTS_PROVIDER", "none")
    monkeypatch.setenv("DEV_LOGIN_ENABLED", "false")
    from forgeflow.config import get_settings

    get_settings.cache_clear()


def test_memory_profile_boots_with_unreachable_postgres(monkeypatch):
    """The memory profile must enter the real lifespan with dead PG — no patch."""
    _point_at_dead_postgres(monkeypatch, backend="memory")

    # NOTE: only the external MCP collaborator is patched. init_pool is NOT
    # patched — that is the whole point of this guard.
    with patch("forgeflow.api.main.get_mcp_tools", new_callable=AsyncMock, return_value=[]):
        from forgeflow.api.main import app

        with TestClient(app) as client:
            response = client.get("/health")
            assert response.status_code == 200, response.text
            assert response.json()["database"] == "unavailable"
            # The memory profile is a first-class "no pool" state.
            assert app.state.pool is None


def test_postgres_profile_fails_fast_with_unreachable_postgres(monkeypatch):
    """Negative control: without the memory short-circuit, startup must fail.

    Same dead PG, but ``STORAGE_BACKEND=postgres`` → ``init_pool()`` is really
    called → asyncpg hits a refused port → lifespan aborts. This is what proves
    the positive test above is not vacuously green.
    """
    _point_at_dead_postgres(monkeypatch, backend="postgres")

    with patch("forgeflow.api.main.get_mcp_tools", new_callable=AsyncMock, return_value=[]):
        from forgeflow.api.main import app

        with pytest.raises(Exception) as excinfo:  # noqa: PT011 — connection error type varies
            with TestClient(app):
                pass

        # A refused TCP connection surfaces as ConnectionRefusedError (an OSError
        # subclass). The OS message is locale-dependent (e.g. Chinese on this
        # box), so assert on the type rather than on the message text.
        assert isinstance(excinfo.value, OSError), (
            f"expected a connection error, got {excinfo.value!r}"
        )
