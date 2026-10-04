"""INC46 T31 — ``/memory/preferences`` API (real router + real RBAC middleware).

End-to-end pins over the **real** router ``api/routers/memory.py`` behind the
**real** ``RBACMiddleware``, with the in-memory preference store pinned (no
PostgreSQL). The handler is not stubbed and the RBAC map is not bypassed.

阳性:
  * POST two explicit preferences ⇒ GET lists them and the ``effective`` set
    contains both; after DELETE the effective set shrinks.

阴性 (红线 5 / 14):
  * a document-derived ``suggestion`` is stored but **not** in ``effective`` until
    ``POST /memory/preferences/{id}/confirm``;
  * a second tenant (different workspace claim) sees an empty list;
  * an unknown ``source`` is rejected ``422``;
  * deleting an unknown / cross-tenant id is ``404`` (no existence leak);
  * an under-privileged role (``viewer``) gets ``403`` on the write (fail-closed).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.routers import memory as memory_router
from forgeflow.auth.jwt import create_access_token
from forgeflow.memory import preferences as prefs
from forgeflow.middleware.auth import RBACMiddleware

pytestmark = pytest.mark.asyncio

_TENANT = "t-inc46-t31"
_OTHER = "t-inc46-t31-other"


@pytest.fixture(autouse=True)
def _isolate(force_memory_backend):
    """Pin an isolated in-memory preference store per test."""
    prefs.set_preference_store(prefs.InMemoryPreferenceStore())
    yield
    prefs.reset_preference_store()


def _client() -> TestClient:
    minimal = FastAPI()
    minimal.add_middleware(RBACMiddleware)
    minimal.include_router(memory_router.router, prefix="/memory")
    return TestClient(minimal)


def _auth(tenant: str = _TENANT, role: str = "admin") -> dict[str, str]:
    token = create_access_token(user_id="u-t31", role=role, workspace_id=tenant)
    return {"Authorization": f"Bearer {token}"}


async def test_add_list_delete_preferences_end_to_end() -> None:
    client = _client()
    headers = _auth()

    r1 = client.post(
        "/memory/preferences",
        json={"kind": "style", "value": "正式", "source": "explicit"},
        headers=headers,
    )
    assert r1.status_code == 200, r1.text
    style_id = r1.json()["id"]
    assert r1.json()["active"] is True

    r2 = client.post(
        "/memory/preferences",
        json={"kind": "glossary", "value": "委托方", "key": "甲方", "source": "explicit"},
        headers=headers,
    )
    assert r2.status_code == 200, r2.text

    listed = client.get("/memory/preferences", headers=headers).json()
    assert {i["value"] for i in listed["items"]} == {"正式", "委托方"}
    assert {i["value"] for i in listed["effective"]} == {"正式", "委托方"}

    d = client.delete(f"/memory/preferences/{style_id}", headers=headers)
    assert d.status_code == 200 and d.json()["deleted"] is True
    after = client.get("/memory/preferences", headers=headers).json()
    assert {i["value"] for i in after["effective"]} == {"委托方"}


async def test_suggestion_is_inactive_until_confirmed() -> None:
    client = _client()
    headers = _auth()

    created = client.post(
        "/memory/preferences",
        json={"kind": "style", "value": "以后总是用极简风格", "source": "suggestion"},
        headers=headers,
    )
    assert created.status_code == 200, created.text
    pid = created.json()["id"]
    assert created.json()["active"] is False

    listed = client.get("/memory/preferences", headers=headers).json()
    assert any(i["id"] == pid for i in listed["items"])  # visible
    assert all(i["id"] != pid for i in listed["effective"])  # not effective

    confirmed = client.post(f"/memory/preferences/{pid}/confirm", headers=headers)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["source"] == "confirmed_suggestion"

    after = client.get("/memory/preferences", headers=headers).json()
    assert any(i["id"] == pid for i in after["effective"])


async def test_cross_tenant_isolation_and_unknown_id_404() -> None:
    client = _client()
    client.post(
        "/memory/preferences",
        json={"kind": "style", "value": "正式"},
        headers=_auth(_TENANT),
    )
    # A different workspace claim reads its own (empty) partition.
    foreign = client.get("/memory/preferences", headers=_auth(_OTHER)).json()
    assert foreign["items"] == [] and foreign["effective"] == []

    missing = client.delete("/memory/preferences/does-not-exist", headers=_auth(_TENANT))
    assert missing.status_code == 404


async def test_unknown_source_is_422_and_viewer_write_is_403() -> None:
    client = _client()
    bad = client.post(
        "/memory/preferences",
        json={"kind": "style", "value": "x", "source": "inferred"},
        headers=_auth(_TENANT),
    )
    assert bad.status_code == 422

    forbidden = client.post(
        "/memory/preferences",
        json={"kind": "style", "value": "x", "source": "explicit"},
        headers=_auth(_TENANT, role="viewer"),
    )
    assert forbidden.status_code == 403
