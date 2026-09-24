"""INC7 — ``GET /context`` (the read side of ``context_build_stats``).

Two things are pinned here:

  1. the **payload** — honest "no data" before any build, and the correct
     numbers after one (the field names match ``get_build_stats()``);
  2. the **RBAC gate** — a token is required (401); a read-only ``viewer`` is
     refused (403, it holds no ``read:memory``); roles that do hold
     ``read:memory`` pass.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.routers import context as context_router
from forgeflow.auth.jwt import create_access_token
from forgeflow.experience.context_builder import ContextBundle
from forgeflow.middleware.auth import RBACMiddleware
from forgeflow.observability.context_stats import record_context_build, reset_build_stats


def _app(*, with_rbac: bool) -> TestClient:
    """A minimal app carrying only the context router (optionally RBAC-gated)."""
    mini = FastAPI()
    if with_rbac:
        mini.add_middleware(RBACMiddleware)
    mini.include_router(context_router.router, prefix="/context")
    return TestClient(mini)


def test_context_stats_payload_reflects_recorded_builds(force_memory_backend):
    """No build ⇒ honest empty; one build ⇒ the real numbers, no invention.

    Pins the memory backend (`force_memory_backend`): the payload contract here
    is the *in-process* aggregate. Under ``STORAGE_BACKEND=postgres`` the same
    route deliberately reads PostgreSQL instead, so this test must declare the
    profile it exercises rather than inherit it from the environment (the exact
    "memory-profile by construction" trap ``conftest.force_memory_backend``
    documents).
    """
    reset_build_stats()
    client = _app(with_rbac=False)

    empty = client.get("/context").json()
    assert empty["has_data"] is False
    assert empty["builds"] == 0
    assert empty["source"] == "memory"
    assert empty["degraded"] is False

    bundle = ContextBundle(
        sections=[{"source": "memory", "ref_id": "m1", "text": "x"}],
        tokens_used=50,
        tokens_raw=200,
        compression_ratio=0.25,
        hit_rate=0.25,
        recalled=4,
    )
    record_context_build(bundle, tenant_id="t-ctx-api", run_id="r-1")

    payload = client.get("/context").json()
    assert payload["builds"] == 1
    assert payload["tokens_raw"] == 200
    assert payload["tokens_used"] == 50
    assert payload["has_data"] is True
    assert payload["recent"][0]["tokens_used"] == 50
    reset_build_stats()


def test_context_requires_a_token_401():
    assert _app(with_rbac=True).get("/context").status_code == 401


def test_context_viewer_is_forbidden_403():
    """``viewer`` holds no ``read:memory`` — a deliberate negative control."""
    token = create_access_token(user_id="viewer-1", role="viewer")
    resp = _app(with_rbac=True).get(
        "/context", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403


def test_context_readers_are_allowed(force_memory_backend):
    """``manager`` / ``sales_rep`` hold ``read:memory`` and reach the route.

    Backend pinned to memory so the asserted ``source`` is deterministic under
    any ``STORAGE_BACKEND`` (the RBAC gate is what this test is really about).
    """
    for role in ("manager", "sales_rep"):
        token = create_access_token(user_id=f"{role}-1", role=role)
        resp = _app(with_rbac=True).get(
            "/context", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200, f"{role} should reach /context: {resp.status_code}"
        assert resp.json()["source"] == "memory"
