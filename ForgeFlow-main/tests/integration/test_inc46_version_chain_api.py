"""INC46 T25 — 版本链 API（real router + real RBAC middleware）。

端到端钉住 **真实** 路由 ``api/routers/artifact_review.py`` 背后的 **真实**
``RBACMiddleware``，store 全部钉成内存实现（无 PostgreSQL）。handler 不被 stub，
RBAC 映射不被绕过。

阳性:
  * ``POST /artifacts/{id}/revert`` 产出**新** pending 版本（历史保留）；
  * ``GET  /artifacts/{id}/versions`` 返回 head + 全部版本 + 全部边；
  * ``GET  /artifacts/{id}/compare?a=&b=`` 返回两版本 diff。

阴性 (红线 5 / 6 / 11):
  * 陈旧 base ⇒ 409（乐观锁）；
  * 回退到不存在的版本 ⇒ 404；
  * 跨租户读空 / 跨租户回退 ⇒ 403（不写影子副本）；
  * viewer 无 write:skills ⇒ 403；未认证 ⇒ 401。
"""

from __future__ import annotations

import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.routers import artifact_review as artifact_router
from forgeflow.auth.jwt import create_access_token
from forgeflow.documents import review_store as rs
from forgeflow.documents import version_chain as vc
from forgeflow.middleware.auth import RBACMiddleware
from forgeflow.outcomes.store import InMemoryOutcomeStore, reset_outcome_store, set_outcome_store

_TENANT = "t-inc46-t25-api"
_OTHER = "t-inc46-t25-api-other"
_ART = "art-t25-api"


@pytest.fixture(autouse=True)
def _isolate(force_memory_backend):
    """Pin isolated in-memory artifact / chain / outcome stores per test."""
    rs.set_artifact_review_store(rs.InMemoryArtifactReviewStore())
    vc.set_version_chain_store(vc.InMemoryVersionChainStore())
    set_outcome_store(InMemoryOutcomeStore())
    yield
    rs.reset_artifact_review_store()
    vc.reset_version_chain_store()
    reset_outcome_store()


def _docx(paragraphs: list[str]) -> bytes:
    import docx  # noqa: PLC0415 — test-only dependency

    doc = docx.Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _client() -> TestClient:
    minimal = FastAPI()
    minimal.add_middleware(RBACMiddleware)
    minimal.include_router(artifact_router.router, prefix="/artifacts")
    return TestClient(minimal)


def _auth(tenant: str = _TENANT, role: str = "admin") -> dict[str, str]:
    token = create_access_token(user_id="u-t25", role=role, workspace_id=tenant)
    return {"Authorization": f"Bearer {token}"}


def _seed_committed(content: bytes) -> rs.ArtifactVersion:
    store = rs.get_artifact_review_store()
    version, _review = store.create_pending_version(
        _TENANT, artifact_id=_ART, content=content, fmt="docx"
    )
    committed, _ = store.approve(_TENANT, _ART, version.version, actor="human-1")
    return committed


# --------------------------------------------------------------------------- #
# 阳性                                                                          #
# --------------------------------------------------------------------------- #
def test_revert_endpoint_mints_a_new_pending_version() -> None:
    _seed_committed(_docx(["第一段", "第二段"]))
    _seed_committed(_docx(["第一段", "第二段-改写"]))
    client = _client()

    reverted = client.post(
        f"/artifacts/{_ART}/revert", json={"to_version": 1, "run_id": "r-t25"}, headers=_auth()
    )
    assert reverted.status_code == 200, reverted.text
    body = reverted.json()
    assert body["version"] == 3  # a NEW version, not an overwrite
    assert body["state"] == "pending"  # 红线 11: still needs approval
    assert body["base_version"] == 2  # parent = the head at revert time
    assert body["approval_id"]  # a real approval id

    chain = client.get(f"/artifacts/{_ART}/versions", headers=_auth())
    assert chain.status_code == 200, chain.text
    view = chain.json()
    assert view["head_version"] == 2  # the revert is still pending ⇒ head unchanged
    assert len(view["versions"]) == 3
    revert_edges = [e for e in view["edges"] if e["edge_kind"] == vc.EDGE_REVERT]
    assert len(revert_edges) == 1
    assert revert_edges[0]["source_version"] == 1
    assert revert_edges[0]["parent_version"] == 2
    assert revert_edges[0]["child_version"] == 3


def test_compare_endpoint_diffs_two_versions() -> None:
    _seed_committed(_docx(["第一段", "第二段"]))
    _seed_committed(_docx(["第一段", "第二段-改写"]))
    client = _client()

    out = client.get(f"/artifacts/{_ART}/compare", params={"a": 1, "b": 2}, headers=_auth())
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["format"] == "docx"
    assert body["counts"]["modified"] >= 1


def test_revert_does_not_destroy_history() -> None:
    first = _docx(["原始"])
    _seed_committed(first)
    _seed_committed(_docx(["改写"]))
    client = _client()
    client.post(f"/artifacts/{_ART}/revert", json={"to_version": 1}, headers=_auth())

    store = rs.get_artifact_review_store()
    assert store.read_content(_TENANT, _ART, 1) == first  # byte-identical original
    assert {v.version for v in store.list_versions(_TENANT, _ART)} == {1, 2, 3}


# --------------------------------------------------------------------------- #
# 阴性                                                                          #
# --------------------------------------------------------------------------- #
def test_revert_with_a_stale_base_conflicts() -> None:
    _seed_committed(_docx(["v1"]))
    _seed_committed(_docx(["v2"]))
    client = _client()
    conflict = client.post(
        f"/artifacts/{_ART}/revert",
        json={"to_version": 1, "base_version": 1},
        headers=_auth(),
    )
    assert conflict.status_code == 409


def test_revert_to_an_unknown_version_is_404() -> None:
    _seed_committed(_docx(["v1"]))
    client = _client()
    missing = client.post(f"/artifacts/{_ART}/revert", json={"to_version": 42}, headers=_auth())
    assert missing.status_code == 404


def test_cross_tenant_is_fail_closed() -> None:
    _seed_committed(_docx(["v1"]))
    client = _client()
    # Read: the foreign tenant's chain is empty (no existence leak).
    foreign = client.get(f"/artifacts/{_ART}/versions", headers=_auth(_OTHER))
    assert foreign.status_code == 200
    assert foreign.json()["versions"] == [] and foreign.json()["head_version"] is None
    # Write: reverting a foreign artifact is refused, never a shadow copy.
    denied = client.post(f"/artifacts/{_ART}/revert", json={"to_version": 1}, headers=_auth(_OTHER))
    assert denied.status_code == 403


def test_viewer_is_refused_by_the_middleware() -> None:
    _seed_committed(_docx(["v1"]))
    client = _client()
    # viewer holds no write:skills ⇒ POST /artifacts/... is fail-closed.
    denied = client.post(
        f"/artifacts/{_ART}/revert", json={"to_version": 1}, headers=_auth(role="viewer")
    )
    assert denied.status_code == 403


def test_unauthenticated_is_401() -> None:
    assert _client().get(f"/artifacts/{_ART}/versions").status_code == 401
