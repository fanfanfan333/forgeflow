import os, sys
sys.path.insert(0, r"D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台\ForgeFlow-main")
os.environ["STORAGE_BACKEND"] = "memory"
os.environ["LLM_PROVIDER"] = "mock"
os.environ["EMBEDDING_PROVIDER"] = "mock"
os.environ["API_SECRET_KEY"] = "qa-secret-key-1234567890"
os.environ["DEV_LOGIN_ENABLED"] = "true"
os.environ["DEV_LOGIN_PASSWORD"] = "qa-dev"

from forgeflow.api.main import app
from starlette.routing import Route, Mount, WebSocketRoute

print("len(app.routes) =", len(app.routes))
print("len(app.router.routes) =", len(app.router.routes))
rows = []
for r in app.router.routes:
    t = type(r).__name__
    path = getattr(r, "path", None)
    methods = getattr(r, "methods", None)
    rows.append((t, path, methods))
print("collected", len(rows))
for t, path, methods in rows:
    ms = ",".join(sorted(methods)) if methods else "-"
    print(f"{t:15} {ms:20} {path}")
