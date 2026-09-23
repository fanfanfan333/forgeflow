"""Fail-closed route coverage (SECURITY_AUDIT.md API5).

The RBAC middleware denies any route that is not in ``ROUTE_PERMISSION_MAP``
(``unmapped_route_default_deny``). This test pins the invariant: every route the
app actually serves is either **mapped** (in the table, longest-prefix match) or
**intentionally open** (health / docs / auth bootstrap). It also guards the real
defect this increment fixed: the three ``POST /marketplace/skills/*`` routes were
unmapped and therefore 403 for everyone.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.main import app
from forgeflow.api.routers import cost as cost_router
from forgeflow.auth.jwt import create_access_token
from forgeflow.middleware.auth import RBACMiddleware
from forgeflow.rbac.policies import ROUTE_PERMISSION_MAP


def _all_routes() -> set[tuple[str, str]]:
    """(METHOD, path) for every route the app exposes, schema + non-schema."""
    routes: set[tuple[str, str]] = set()
    for path, methods in app.openapi().get("paths", {}).items():
        for method in methods:
            routes.add((method.upper(), path))
    for route in app.routes:
        if hasattr(route, "methods") and hasattr(route, "path"):
            for method in getattr(route, "methods", []) or []:
                routes.add((method, route.path))
    return routes


def test_every_route_is_mapped_or_intentionally_open():
    unresolved = sorted(
        (method, path)
        for (method, path) in _all_routes()
        if RBACMiddleware._resolve_permission(method, path) == ("", "")
        and not RBACMiddleware._is_open(path)
    )
    assert unresolved == [], f"routes reachable by nobody (fail-closed 403): {unresolved}"


def test_marketplace_post_routes_are_now_mapped():
    # Regression for the real defect: publish/install/rate were unmapped.
    for path in (
        "/marketplace/skills/publish",
        "/marketplace/skills/listing-1/install",
        "/marketplace/skills/listing-1/rate",
    ):
        assert RBACMiddleware._resolve_permission("POST", path) == ("write", "marketplace")
    # The longer, more specific prefix still wins for templates/refresh.
    assert RBACMiddleware._resolve_permission("POST", "/marketplace/templates/refresh") == (
        "write",
        "marketplace",
    )


def test_new_cost_audit_skills_routes_are_mapped():
    assert RBACMiddleware._resolve_permission("GET", "/cost/board") == ("read", "metrics")
    assert RBACMiddleware._resolve_permission("GET", "/cost/savings") == ("read", "metrics")
    assert RBACMiddleware._resolve_permission("GET", "/audit/export") == ("read", "audit")
    assert RBACMiddleware._resolve_permission("GET", "/skills/evolution-advice") == (
        "read",
        "skills",
    )


def test_manager_role_can_write_marketplace():
    from forgeflow.rbac.enforcer import RBACEnforcer

    assert RBACEnforcer().check("manager", "write", "marketplace")
    assert ROUTE_PERMISSION_MAP[("POST", "/marketplace")] == ("write", "marketplace")


# --------------------------------------------------------------------------- #
# Middleware behaviour — no token ⇒ 401 on a mapped route                       #
# --------------------------------------------------------------------------- #

def _rbac_client() -> TestClient:
    minimal = FastAPI()
    minimal.add_middleware(RBACMiddleware)
    minimal.include_router(cost_router.router, prefix="/cost")
    return TestClient(minimal)


def test_unauthenticated_request_is_401():
    response = _rbac_client().get("/cost/board")
    assert response.status_code == 401


def test_authenticated_admin_reaches_cost_board():
    token = create_access_token(user_id="admin-1", role="admin")
    response = _rbac_client().get(
        "/cost/board", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
