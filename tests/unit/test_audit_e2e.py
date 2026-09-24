"""End-to-end proof that the audit producer and consumer are wired together.

This is not two half-tests: it drives the **real middleware + real HTTP** path
(``AuditMiddleware`` on an app with the real routers), then reads the result
back through the consumer API (``/audit/search`` + ``/audit/export``). It also
proves the offline (memory) sink, the field alignment with the PostgreSQL sink,
and that non-success requests are recorded.

Middleware order mirrors ``api/main.py``:

    SecurityHeaders → Audit → RBAC → RateLimit → Security → handler

so ``AuditMiddleware`` sits **outside** ``RBACMiddleware`` and ``SecurityHeaders``
sits outermost. Two consequences are asserted explicitly below because they are
security-relevant and easy to regress:

  * a rejected attempt (RBAC 401/403) flows back through ``AuditMiddleware`` and
    is recorded with ``outcome == "denied"`` (SECURITY_AUDIT.md API5), and
  * the hardening headers still land on those 401/403 responses, and
  * ``RateLimitMiddleware`` still runs *inside* RBAC, so its bucket key is the
    already-verified ``request.state.user_id`` (two users from the same IP get
    independent buckets).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.routers import audit as audit_router
from forgeflow.api.routers import cost as cost_router
from forgeflow.api.routers import marketplace as marketplace_router
from forgeflow.api.routers import skills as skills_router
from forgeflow.api.routers.audit import _RING, clear_audit_ring
from forgeflow.auth.jwt import create_access_token
from forgeflow.middleware import audit as audit_mw
from forgeflow.middleware.audit import AuditMiddleware
from forgeflow.middleware.auth import RBACMiddleware
from forgeflow.middleware.rate_limit import _LIMITS, RateLimitMiddleware
from forgeflow.middleware.security import SecurityMiddleware
from forgeflow.middleware.security_headers import SecurityHeadersMiddleware


def _include_routers(app: FastAPI, *, with_cost: bool = False) -> None:
    app.include_router(skills_router.router, prefix="/skills")
    app.include_router(marketplace_router.router, prefix="/marketplace")
    app.include_router(audit_router.router, prefix="/audit")
    if with_cost:
        app.include_router(cost_router.router, prefix="/cost")


def _build_app() -> FastAPI:
    """Auth-relevant slice of the stack, same *add-order* as ``api/main.py``.

    RBAC is added before Audit ⇒ Audit is OUTERMOST on the request path
    (``Audit → RBAC → handler``), exactly like the shipped app. A rejected
    401/403 therefore still flows back through Audit and is recorded.
    """
    app = FastAPI()
    app.add_middleware(RBACMiddleware)  # inner
    app.add_middleware(AuditMiddleware)  # outer → SecurityHeaders is omitted here
    _include_routers(app)
    return app


def _build_full_app() -> FastAPI:
    """Full middleware stack, identical add-order to ``forgeflow.api.main``.

    Request flow: ``SecurityHeaders → Audit → RBAC → RateLimit → Security →
    handler``. Used to prove the security-header + rate-limit invariants that
    need those two middlewares present.
    """
    app = FastAPI()
    app.add_middleware(SecurityMiddleware)  # innermost
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(RBACMiddleware)
    app.add_middleware(AuditMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)  # outermost
    _include_routers(app, with_cost=True)
    return app


def _skip_audit_reads(monkeypatch) -> None:
    # Exclude the audit READ endpoints from auditing so a search/export
    # comparison observes an identical ring state — otherwise each read request
    # self-audits and shifts the row count. Only the test fixture is affected;
    # the shipped _SKIP_AUDIT set is untouched.
    monkeypatch.setattr(
        audit_mw,
        "_SKIP_AUDIT",
        set(audit_mw._SKIP_AUDIT) | {"/audit/search", "/audit/export"},
    )


@pytest.fixture(autouse=True)
def _force_memory_audit_profile(force_memory_backend):
    """This suite is *memory-profile by construction* — pin it, don't inherit.

    It builds its own app (``_build_app`` / ``_build_full_app``) with no lifespan,
    so ``request.app.state.pool is None``. In the *write* path
    ``AuditMiddleware`` → ``write_audit_entry(pool=None)`` then falls back to
    ``middleware.audit._available_pool()``, which returns ``database._pool``
    **only when ``storage_backend == 'postgres'``** — and hitting ``/skills``
    initialises that global pool. The *read* path (``/audit/search`` → ``_soft_pool``)
    only ever looks at ``app.state.pool`` (None) and reads the ring. So under
    ``STORAGE_BACKEND=postgres`` the two sinks diverge (PG write, ring read) and
    every row-count assertion sees ``total == 0``.

    Pinning the memory backend — the documented ``force_memory_backend`` convention
    in ``tests/conftest.py`` for "memory-by-construction" suites — makes producer
    and consumer both use the ring buffer, independent of the ambient
    ``STORAGE_BACKEND``. No product code is touched: the two sinks and their field
    contract are unchanged.
    """
    yield force_memory_backend


@pytest.fixture
def client(monkeypatch) -> TestClient:
    _skip_audit_reads(monkeypatch)
    clear_audit_ring()
    # Entering the context manager keeps ONE event loop (anyio portal) alive for
    # the whole test. Starlette otherwise spins a *fresh* portal per request, so a
    # module-global asyncpg pool created on request N's loop gets reused on
    # request N+1's closed loop → "Event loop is closed" / "another operation is
    # in progress". This is the same isolation intent as the per-test pool reset
    # in conftest.py, applied at the request-loop granularity the TestClient owns.
    with TestClient(_build_app()) as test_client:
        yield test_client


@pytest.fixture
def full_client(monkeypatch) -> TestClient:
    _skip_audit_reads(monkeypatch)
    clear_audit_ring()
    with TestClient(_build_full_app()) as test_client:
        yield test_client


def _auth() -> dict[str, str]:
    token = create_access_token(user_id="manager-1", role="manager")
    return {"Authorization": f"Bearer {token}"}


def _viewer_auth() -> dict[str, str]:
    token = create_access_token(user_id="viewer-1", role="viewer")
    return {"Authorization": f"Bearer {token}"}


def _search(client: TestClient) -> list[dict]:
    return client.get("/audit/search", headers=_auth()).json()["items"]


# --------------------------------------------------------------------------- #
# ① producer → consumer: a real request is queryable via /audit/search          #
# --------------------------------------------------------------------------- #

def test_real_request_is_queryable_via_search(client):
    ok = client.get("/skills", headers=_auth())
    assert ok.status_code == 200

    search = client.get("/audit/search", headers=_auth())
    assert search.status_code == 200
    body = search.json()

    assert body["total"] == 1
    item = body["items"][0]
    assert item["resource"] == "/skills"
    assert item["action"] == "GET"
    assert item["outcome"] == "allowed"
    assert item["role"] == "manager"


# --------------------------------------------------------------------------- #
# ② export row count == search count                                           #
# --------------------------------------------------------------------------- #

def test_export_rows_equal_search_count(client):
    client.get("/skills", headers=_auth())
    client.post(
        "/marketplace/skills/publish", headers=_auth(), json={"skill_id": "does-not-exist"}
    )

    search = client.get("/audit/search", headers=_auth()).json()
    export = client.get("/audit/export", headers=_auth(), params={"format": "csv"})
    assert export.status_code == 200

    data_rows = [line for line in export.text.splitlines()[1:] if line.strip()]
    assert len(data_rows) == search["total"] == 2


# --------------------------------------------------------------------------- #
# ③ failed / rejected requests are recorded with the right outcome             #
# --------------------------------------------------------------------------- #

def test_failed_request_is_recorded_with_correct_outcome(client):
    client.get("/skills", headers=_auth())  # → allowed
    rejected = client.post(
        "/marketplace/skills/publish", headers=_auth(), json={"skill_id": "does-not-exist"}
    )
    assert rejected.status_code == 404  # handler-level rejection, reaches Audit

    items = client.get("/audit/search", headers=_auth()).json()["items"]
    outcomes = {(i["resource"], i["action"]): i["outcome"] for i in items}

    assert outcomes[("/skills", "GET")] == "allowed"
    assert outcomes[("/marketplace/skills/publish", "POST")] == "error"


# --------------------------------------------------------------------------- #
# Field alignment with the PostgreSQL sink (same columns, same metadata keys)  #
# --------------------------------------------------------------------------- #

def test_memory_entry_fields_align_with_pg_columns(client):
    client.get("/skills", headers=_auth())
    data = client.get("/audit/export", headers=_auth(), params={"format": "json"}).json()

    item = data["items"][0]
    assert set(item) == {
        "id",
        "timestamp",
        "user_id",
        "role",
        "action",
        "resource",
        "resource_id",
        "outcome",
        "request_id",
        "metadata",
    }
    assert item["request_id"]
    assert item["action"] == "GET"
    assert item["resource"] == "/skills"
    # Non-UUID subject ("manager-1") → user_id column is NULL, exactly like the
    # PG branch; the raw identity survives in metadata.user_id_str.
    assert item["user_id"] is None
    assert set(item["metadata"]) == {
        "status_code",
        "latency_ms",
        "user_agent",
        "user_id_str",
        "client_ip",
    }
    assert item["metadata"]["status_code"] == 200
    assert item["metadata"]["user_id_str"] == "manager-1"


def test_skip_audit_paths_are_not_recorded(client):
    # /health is in _SKIP_AUDIT → no audit row even in the memory profile.
    client.get("/health")
    assert len(_RING) == 0


# --------------------------------------------------------------------------- #
# SECURITY_AUDIT.md API5: rejected attempts MUST be auditable                  #
# --------------------------------------------------------------------------- #

def test_rbac_401_is_audited_as_denied(client):
    """A missing/invalid token is rejected by RBACMiddleware, which now runs
    INSIDE AuditMiddleware, so the 401 flows back through Audit and *is*
    recorded — with ``outcome == "denied"``. Regression guard for the middleware
    order in api/main.py."""
    response = client.get("/skills")  # no token
    assert response.status_code == 401

    items = _search(client)
    assert len(items) == 1
    row = items[0]
    assert row["resource"] == "/skills"
    assert row["action"] == "GET"
    assert row["outcome"] == "denied"
    assert row["metadata"]["status_code"] == 401
    # No identity was ever verified → no user attributed.
    assert row["user_id"] is None


def test_rbac_403_is_audited_as_denied_with_verified_identity(client):
    """An authenticated-but-forbidden request (viewer → write route) is audited
    as ``denied`` AND carries the identity RBAC had already verified."""
    response = client.post(
        "/marketplace/skills/publish", headers=_viewer_auth(), json={"skill_id": "x"}
    )
    assert response.status_code == 403

    items = _search(client)
    assert len(items) == 1
    row = items[0]
    assert row["resource"] == "/marketplace/skills/publish"
    assert row["action"] == "POST"
    assert row["outcome"] == "denied"
    assert row["role"] == "viewer"  # RBAC sets role before the permission check
    assert row["metadata"]["status_code"] == 403


def test_allowed_request_still_reads_allowed(client):
    client.get("/skills", headers=_auth())
    assert _search(client)[0]["outcome"] == "allowed"


# --------------------------------------------------------------------------- #
# SecurityHeaders stays OUTERMOST — 401/403 still carry the hardening headers  #
# --------------------------------------------------------------------------- #

def test_security_headers_present_on_401_and_403(full_client):
    unauthorized = full_client.get("/skills")  # no token → 401
    assert unauthorized.status_code == 401
    assert unauthorized.headers["X-Content-Type-Options"] == "nosniff"
    assert unauthorized.headers["X-Frame-Options"] == "DENY"
    assert "Content-Security-Policy" in unauthorized.headers
    assert unauthorized.headers["Referrer-Policy"] == "no-referrer"

    forbidden = full_client.post(
        "/marketplace/skills/publish", headers=_viewer_auth(), json={"skill_id": "x"}
    )
    assert forbidden.status_code == 403
    assert forbidden.headers["X-Content-Type-Options"] == "nosniff"
    assert forbidden.headers["X-Frame-Options"] == "DENY"
    assert "Content-Security-Policy" in forbidden.headers


# --------------------------------------------------------------------------- #
# RateLimit stays INSIDE RBAC — bucket key is the verified user_id             #
# --------------------------------------------------------------------------- #

def test_rate_limit_is_keyed_by_verified_user_id(full_client):
    """Drain manager-1's read bucket, then show a *different* verified user has
    an independent bucket. If RateLimit ran before RBAC (or keyed on the shared
    client IP) the second user would inherit the exhausted bucket and get 429 —
    so a 200 here proves the key came from ``request.state.user_id``."""
    limit = _LIMITS["read"]
    statuses = []
    for _ in range(limit + 1):
        statuses.append(full_client.get("/skills", headers=_auth()).status_code)
    assert statuses[-1] == 429, f"manager-1 was never throttled: {statuses[-3:]}"
    assert statuses[0] == 200

    # Same client IP, different verified user → fresh bucket.
    viewer = full_client.get("/skills", headers=_viewer_auth())
    assert viewer.status_code == 200


def test_rate_limit_429_is_audited(full_client):
    """The 429 produced INSIDE RBAC still flows back through Audit (Audit is
    outside RateLimit) and is recorded as an ``error`` (not a denial)."""
    for _ in range(_LIMITS["read"] + 1):
        response = full_client.get("/skills", headers=_auth())
    assert response.status_code == 429

    rows = [
        r
        for r in full_client.get("/audit/search", headers=_auth()).json()["items"]
        if r["metadata"]["status_code"] == 429
    ]
    assert rows, "the 429 was not audited"
    assert rows[0]["outcome"] == "error"
