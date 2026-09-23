"""QA D1.4 — runtime 403 regression for POST /marketplace/skills/*.

Builds a real ASGI app (RBACMiddleware + marketplace router), overrides the
tenant/user deps with canned values, and actually POSTs with an admin token.
The original defect: these routes were unmapped => RBACMiddleware returned 403
for EVERYONE. Now an admin token must NOT get a 403.
"""
import sys

sys.path.insert(0, r"D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台\ForgeFlow-main")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.dependencies import get_current_user
from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.routers import marketplace
from forgeflow.auth.jwt import create_access_token
from forgeflow.middleware.auth import RBACMiddleware
from forgeflow.rbac.models import UserContext


def build_client(role: str, user_id: str = "admin-1"):
    app = FastAPI()
    app.add_middleware(RBACMiddleware)
    app.include_router(marketplace.router, prefix="/marketplace")
    app.dependency_overrides[resolve_tenant] = lambda: "tenant-a"
    app.dependency_overrides[get_current_user] = lambda: UserContext(user_id=user_id, role=role)
    token = create_access_token(user_id=user_id, role=role)
    client = TestClient(app, raise_server_exceptions=False)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


admin = build_client("admin")
print("--- admin token ---")
r = admin.post("/marketplace/skills/publish", json={"skill_id": "nope", "shared": False})
print("POST /marketplace/skills/publish        ->", r.status_code, r.text[:200])
r = admin.post("/marketplace/skills/listing-1/install")
print("POST /marketplace/skills/x/install      ->", r.status_code, r.text[:200])
r = admin.post("/marketplace/skills/listing-1/rate", json={"score": 4})
print("POST /marketplace/skills/x/rate         ->", r.status_code, r.text[:200])
r = admin.post("/marketplace/templates/refresh")
print("POST /marketplace/templates/refresh     ->", r.status_code, r.text[:200])

print("--- no token (should be 401) ---")
noauth = TestClient(
    build_client("admin").app, raise_server_exceptions=False
)
# strip the auth header for a clean 401 probe
noauth.headers.pop("Authorization", None)
r = noauth.post("/marketplace/skills/publish", json={"skill_id": "nope"})
print("POST /marketplace/skills/publish no-tok ->", r.status_code, r.text[:120])

print("--- viewer token (should be 403: lacks write:marketplace) ---")
viewer = build_client("viewer", user_id="viewer-1")
r = viewer.post("/marketplace/skills/publish", json={"skill_id": "nope"})
print("POST /marketplace/skills/publish viewer ->", r.status_code, r.text[:160])
