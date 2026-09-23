"""INC4 T9 — password login must work in the offline (memory) profile.

Background (``目标.md`` §L5 / ``docs/sop/08-FRAMEWORK-ALIGNMENT.md``)
---------------------------------------------------------------------
``POST /auth/login`` depended directly on ``api.dependencies.get_pool``, which
raises ``503`` when there is no PostgreSQL pool. In the offline profile
(``STORAGE_BACKEND=memory``) the app deliberately boots with ``pool = None``
(``api/main.py`` lifespan), so the SPA could **never get past the login screen
without a database** — the whole offline story stopped at the door.

The fix follows the project's established *repository abstraction + dual
implementation* pattern (same shape as ``graph/checkpointer.py`` picking
``AsyncPostgresSaver`` vs ``InMemorySaver``):

  * ``auth/store.py::get_auth_store`` resolves a **credential store** instead of
    a raw pool — ``PgAuthStore`` when a pool exists, ``MemoryAuthAdapter`` in the
    offline profile, ``503`` otherwise (never a silent downgrade);
  * ``api/routers/auth.py`` keeps **one** code path over that store — no
    ``if offline:`` fork, so the two profiles cannot drift.

Security posture pinned here on purpose
---------------------------------------
* Same Argon2id hashing, same refresh-token rotation + reuse detection offline
  as online — the offline store is a *storage swap*, not a weaker auth mode.
* The memory store is reachable **only** when ``storage_backend == "memory"``
  **and** the deployment is not prod-shaped. A production deployment must fail
  closed (503) rather than fall back to an in-process credential store.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr

from forgeflow.api.routers import auth as auth_router
from forgeflow.auth import memory_store, passwords
from forgeflow.auth.demo_users import DEMO_USERS

_DEV_PASSWORD = "offline-dev-pw"


@pytest.fixture(autouse=True)
def _clean_auth_state(force_memory_backend, monkeypatch):
    """Every case starts from an empty memory store, a clean rate-limit bucket,
    and a known dev password."""
    monkeypatch.setattr(force_memory_backend, "dev_login_enabled", True)
    monkeypatch.setattr(
        force_memory_backend, "dev_login_password", SecretStr(_DEV_PASSWORD)
    )
    memory_store.reset_memory_store()
    auth_router._attempts.clear()
    yield
    memory_store.reset_memory_store()
    auth_router._attempts.clear()


def _auth_client() -> TestClient:
    """An ASGI app with **no** ``state.pool`` — the offline profile."""
    app = FastAPI()
    app.include_router(auth_router.router, prefix="/auth")
    assert getattr(app.state, "pool", None) is None
    return TestClient(app)


def _seed(password: str = _DEV_PASSWORD, workspace: str | None = None) -> None:
    asyncio.run(memory_store.seed_demo_users(password, workspace_id=workspace))


def _login(workspace_id: str | None = None, user: str = "admin") -> TestClient.response_class:
    _seed()
    body: dict = {"user_id": user, "password": _DEV_PASSWORD}
    if workspace_id is not None:
        body["workspace_id"] = workspace_id
    auth_router._attempts.clear()
    return _auth_client().post("/auth/login", json=body)


# --------------------------------------------------------------------------- #
# Store eligibility — the fail-closed boundary
# --------------------------------------------------------------------------- #
class TestMemoryAuthAllowed:
    def test_allowed_offline(self, force_memory_backend):
        assert memory_store.memory_auth_allowed(force_memory_backend) is True

    def test_denied_in_production_shaped_deployment(self, force_memory_backend, monkeypatch):
        """Prod + memory must never fall back to an in-process store."""
        monkeypatch.setattr(force_memory_backend, "otel_environment", "production")
        assert force_memory_backend.is_production() is True
        assert memory_store.memory_auth_allowed(force_memory_backend) is False

    def test_denied_on_postgres_backend(self, force_memory_backend, monkeypatch):
        """With a real backend configured, absence of a pool is a bug — not a
        licence to invent an in-memory store."""
        monkeypatch.setattr(force_memory_backend, "storage_backend", "postgres")
        assert memory_store.memory_auth_allowed(force_memory_backend) is False


# --------------------------------------------------------------------------- #
# Offline login over the ASGI layer (no pool anywhere)
# --------------------------------------------------------------------------- #
class TestOfflineLogin:
    def test_login_without_a_database_returns_200_not_503(self):
        """The headline regression: the memory profile could not log in at all."""
        response = _login()

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["access_token"]
        assert body["refresh_token"]
        assert body["token_type"] == "bearer"
        assert body["role"] == DEMO_USERS["admin"]

    def test_login_rejects_a_wrong_password(self):
        _seed()
        auth_router._attempts.clear()
        response = _auth_client().post(
            "/auth/login", json={"user_id": "admin", "password": "wrong"}
        )

        assert response.status_code == 401
        assert response.json()["detail"] == "invalid credentials"

    def test_login_rejects_an_unknown_user(self):
        _seed()
        auth_router._attempts.clear()
        response = _auth_client().post(
            "/auth/login", json={"user_id": "ghost", "password": _DEV_PASSWORD}
        )

        assert response.status_code == 401

    def test_every_seeded_demo_user_can_log_in(self):
        _seed()
        for username in DEMO_USERS:
            # The shared limiter allows only 5 attempts / 60s per client IP.
            auth_router._attempts.clear()
            response = _auth_client().post(
                "/auth/login", json={"user_id": username, "password": _DEV_PASSWORD}
            )
            assert response.status_code == 200, f"{username} -> {response.status_code}"

    def test_the_roster_is_shared_with_the_postgres_path(self):
        """One source of truth: offline must never drift to a different user set
        than the database profile."""
        from forgeflow.auth import demo_users

        assert set(DEMO_USERS) == set(demo_users.DEMO_USERS)
        assert set(DEMO_USERS) == {"admin", "manager-1", "rep-1", "viewer-1"}


# --------------------------------------------------------------------------- #
# Password hashing parity — the offline store must not weaken credentials
# --------------------------------------------------------------------------- #
class TestPasswordParity:
    def test_offline_store_hashes_with_argon2id_not_plaintext(self):
        _seed()
        row = asyncio.run(
            memory_store.get_memory_store().get_by_username("admin")
        )

        assert row["password_hash"] != _DEV_PASSWORD
        assert row["password_hash"].startswith("$argon2")
        assert passwords.verify_password(row["password_hash"], _DEV_PASSWORD) is True


# --------------------------------------------------------------------------- #
# Refresh token rotation + reuse detection offline
# --------------------------------------------------------------------------- #
class TestOfflineRefreshTokenRotation:
    def test_refresh_returns_a_new_pair_offline(self):
        first = _login()
        assert first.status_code == 200
        response = _auth_client().post(
            "/auth/refresh", json={"refresh_token": first.json()["refresh_token"]}
        )

        assert response.status_code == 200, response.text
        new_tokens = response.json()
        assert new_tokens["access_token"]
        assert new_tokens["refresh_token"] != first.json()["refresh_token"]

    def test_refresh_token_reuse_is_detected_offline(self):
        """Same guarantee as the PostgreSQL path: replaying a rotated token 401s.
        A memory store without this would be a silent security downgrade."""
        first = _login()
        assert first.status_code == 200
        presented = first.json()["refresh_token"]
        client = _auth_client()

        assert client.post("/auth/refresh", json={"refresh_token": presented}).status_code == 200
        replay = client.post("/auth/refresh", json={"refresh_token": presented})
        assert replay.status_code == 401


# --------------------------------------------------------------------------- #
# Workspace claim — C-4 must not reopen offline
# --------------------------------------------------------------------------- #
class TestOfflineWorkspaceClaim:
    def test_member_claim_is_honoured(self):
        ws = str(uuid.uuid4())
        _seed(workspace=ws)
        auth_router._attempts.clear()
        response = _auth_client().post(
            "/auth/login", json={"user_id": "admin", "password": _DEV_PASSWORD, "workspace_id": ws}
        )

        assert response.status_code == 200, response.text
        assert response.json()["workspace_id"] == ws

    def test_unverifiable_claim_is_refused_not_allowed(self):
        """Offline there is no ``workspace_members`` table — exactly the
        pre-migration case in which the PG path denies. Deny here too; allowing
        every claim would silently reopen C-4 in the memory profile."""
        _seed(workspace=str(uuid.uuid4()))
        auth_router._attempts.clear()
        response = _auth_client().post(
            "/auth/login",
            json={
                "user_id": "admin",
                "password": _DEV_PASSWORD,
                "workspace_id": str(uuid.uuid4()),
            },
        )

        assert response.status_code == 403


# --------------------------------------------------------------------------- #
# get_auth_store resolution
# --------------------------------------------------------------------------- #
def _request(pool):
    class _App:
        class state:
            pass

    _App.state.pool = pool

    class _Req:
        app = _App()

    return _Req()


class TestAuthStoreResolution:
    def test_no_pool_and_postgres_backend_is_503_not_memory(self, force_memory_backend, monkeypatch):
        """A misconfigured deployment must fail loudly, never authenticate
        against an invented in-process roster."""
        from forgeflow.auth.store import get_auth_store

        monkeypatch.setattr(force_memory_backend, "storage_backend", "postgres")

        with pytest.raises(HTTPException) as exc:
            asyncio.run(get_auth_store(_request(None)))
        assert exc.value.status_code == 503

    def test_no_pool_and_offline_backend_resolves_the_memory_store(self, force_memory_backend):
        from forgeflow.auth.store import get_auth_store

        store = asyncio.run(get_auth_store(_request(None)))
        assert store.backend == "memory"

    def test_a_pool_wins_over_the_memory_store(self, force_memory_backend):
        """The PostgreSQL path stays authoritative whenever a pool exists."""
        from forgeflow.auth.store import get_auth_store

        store = asyncio.run(get_auth_store(_request(object())))
        assert store.backend == "postgres"
