"""INC34 — skill spec ``io_schema`` validation, skill export, candidate threshold.

Three NEW behaviours are pinned here (all additive; nothing existing changed):

  * a *declared* ``io_schema`` is structurally validated and a broken one is a
    **400** with a Chinese, human-readable reason — while an undeclared
    ``io_schema`` (or the documented ``{"input": {...}, "output": {...}}``
    convention) is untouched (backward compatibility);
  * ``GET /skills/{id}/export`` returns a self-describing skill document and is
    reachable via the ``/skills`` RBAC prefix (UNMAPPED stays 0);
  * an ``insufficient`` skill candidate reports the **configured** required
    experience count so the UI never has to invent the number.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.routers import skills as skills_router
from forgeflow.auth.jwt import create_access_token
from forgeflow.config import get_settings
from forgeflow.middleware.auth import RBACMiddleware
from forgeflow.skills.spec_validation import validate_io_schema

# A valid spec using the compiler's own convention — must never be rejected.
_GOOD_SPEC = {
    "prompt": "为「数据分析」类任务生成可复用技能。",
    "steps": ["检索上下文", "执行任务"],
    "tools": ["data.query", "report.render"],
    "io_schema": {"input": {"intent": "string"}, "output": {"summary": "string"}},
}


def _tenant() -> str:
    return f"t-inc34-{uuid.uuid4().hex[:8]}"


def _auth(role: str = "manager", tenant: str | None = None) -> dict[str, str]:
    token = create_access_token(
        user_id=f"{role}-1", role=role, workspace_id=tenant or _tenant()
    )
    return {"Authorization": f"Bearer {token}"}


def _client() -> TestClient:
    app = FastAPI()
    app.add_middleware(RBACMiddleware)
    app.include_router(skills_router.router, prefix="/skills")
    app.include_router(skills_router.candidates_router, prefix="/skill-candidates")
    return TestClient(app)


# --------------------------------------------------------------------------- #
# 1. Pure validator                                                            #
# --------------------------------------------------------------------------- #

def test_validator_accepts_compiler_convention():
    assert validate_io_schema(_GOOD_SPEC["io_schema"]) == []


def test_validator_accepts_json_schema_subset_and_free_form():
    assert validate_io_schema({"type": "object", "properties": {"a": {"type": "string"}}}) == []
    # Unknown keys are ignored → existing free-form specs stay valid.
    assert validate_io_schema({"anything": "goes", "n": 1}) == []


def test_validator_rejects_non_object():
    problems = validate_io_schema(["not", "an", "object"])
    assert problems and "必须是对象" in problems[0]


def test_validator_rejects_bad_recognised_keys():
    assert any("type" in p for p in validate_io_schema({"type": 123}))
    assert any("properties" in p for p in validate_io_schema({"properties": []}))
    assert any("required" in p for p in validate_io_schema({"required": "x"}))
    assert any("input" in p for p in validate_io_schema({"input": "nope"}))


def test_validator_bounds_deep_nesting():
    node: dict = {}
    cursor = node
    for _ in range(60):
        child: dict = {}
        cursor["properties"] = {"x": child}
        cursor = child
    assert any("嵌套层级过深" in p for p in validate_io_schema(node))


# --------------------------------------------------------------------------- #
# 2. Endpoint — declared io_schema is validated (400), undeclared is untouched #
# --------------------------------------------------------------------------- #

@pytest.fixture
def _memory(force_memory_backend):
    return force_memory_backend


def _create_skill(client: TestClient, headers: dict[str, str], name: str) -> str:
    resp = client.post("/skills", json={"name": name, "domain": "general"}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def test_invalid_io_schema_is_400_with_chinese_reason(_memory):
    client = _client()
    headers = _auth()
    skill_id = _create_skill(client, headers, f"skill-{uuid.uuid4().hex[:6]}")
    resp = client.post(
        f"/skills/{skill_id}/versions",
        json={"spec": {"io_schema": {"input": ["bad"]}}},
        headers=headers,
    )
    assert resp.status_code == 400, resp.text
    assert "io_schema" in resp.json()["detail"]


def test_valid_io_schema_version_is_created(_memory):
    client = _client()
    headers = _auth()
    skill_id = _create_skill(client, headers, f"skill-{uuid.uuid4().hex[:6]}")
    resp = client.post(
        f"/skills/{skill_id}/versions", json={"spec": _GOOD_SPEC}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["spec"]["io_schema"] == _GOOD_SPEC["io_schema"]


def test_spec_without_io_schema_is_backward_compatible(_memory):
    client = _client()
    headers = _auth()
    skill_id = _create_skill(client, headers, f"skill-{uuid.uuid4().hex[:6]}")
    resp = client.post(
        f"/skills/{skill_id}/versions",
        json={"spec": {"prompt": "任意自由结构", "note": [1, 2, 3]}},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text


# --------------------------------------------------------------------------- #
# 3. Export endpoint + RBAC prefix                                             #
# --------------------------------------------------------------------------- #

def test_export_route_inherits_read_skills_prefix():
    assert RBACMiddleware._resolve_permission("GET", "/skills/abc/export") == (
        "read",
        "skills",
    )


def test_export_returns_skill_and_current_version(_memory):
    client = _client()
    headers = _auth()
    skill_id = _create_skill(client, headers, f"skill-{uuid.uuid4().hex[:6]}")
    client.post(f"/skills/{skill_id}/versions", json={"spec": _GOOD_SPEC}, headers=headers)

    resp = client.get(f"/skills/{skill_id}/export", headers=headers)
    assert resp.status_code == 200, resp.text
    doc = resp.json()
    assert doc["format"] == "forgeflow.skill"
    assert doc["format_version"] == 1
    assert doc["skill"]["id"] == skill_id
    assert doc["version"]["spec"]["prompt"] == _GOOD_SPEC["prompt"]


def test_export_missing_skill_is_404(_memory):
    client = _client()
    resp = client.get("/skills/does-not-exist/export", headers=_auth())
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# 4. Candidate threshold is surfaced honestly (never invented)                 #
# --------------------------------------------------------------------------- #

def test_insufficient_candidate_reports_configured_threshold(_memory):
    client = _client()
    resp = client.post("/skill-candidates", json={"mode": "auto"}, headers=_auth())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "insufficient"
    assert body["required_experiences"] == get_settings().skill_candidate_min_experiences
