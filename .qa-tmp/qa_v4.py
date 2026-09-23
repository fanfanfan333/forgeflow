import os, re, sys
sys.path.insert(0, r"D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台\ForgeFlow-main")
os.environ["STORAGE_BACKEND"] = "memory"
os.environ["LLM_PROVIDER"] = "mock"
os.environ["EMBEDDING_PROVIDER"] = "mock"
os.environ["API_SECRET_KEY"] = "qa-secret-key-1234567890"
os.environ["DEV_LOGIN_ENABLED"] = "true"
os.environ["DEV_LOGIN_PASSWORD"] = "qa-dev"
os.environ["OTEL_ENVIRONMENT"] = "development"

from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

with patch("forgeflow.api.main.init_pool", new_callable=AsyncMock) as mp, \
     patch("forgeflow.api.main.compile_graph", new_callable=AsyncMock) as mg, \
     patch("forgeflow.api.main.get_mcp_tools", new_callable=AsyncMock, return_value=[]):
    mp.return_value = MagicMock()
    mg.return_value = MagicMock()
    from forgeflow.api.main import app
    from forgeflow.middleware.auth import RBACMiddleware, _OPEN_PATHS, _OPEN_PREFIXES
    from forgeflow.config import get_settings

    spec = app.openapi()
    pairs = []
    for p, ops in spec["paths"].items():
        for method in ops.keys():
            pairs.append((method.upper(), p))
    pairs.sort()
    print("TOTAL (method,path) in OpenAPI:", len(pairs))

    def is_open(path):
        if path in _OPEN_PATHS:
            return True
        if any(path.startswith(pre) for pre in _OPEN_PREFIXES):
            return True
        return (path.startswith("/docs") or path.startswith("/redoc")) and get_settings().docs_enabled

    unmapped = []
    for m, p in pairs:
        if is_open(p):
            continue
        action, resource = RBACMiddleware._resolve_permission(m, p)
        if not action:
            unmapped.append((m, p))
    print("UNMAPPED (registered but not in ROUTE_PERMISSION_MAP):", len(unmapped))
    for m, p in unmapped:
        print("   !!", m, p)

    print("\n--- full registered surface ---")
    for m, p in pairs:
        mark = "OPEN" if is_open(p) else ("UNMAPPED" if RBACMiddleware._resolve_permission(m, p)[0] == "" else "mapped")
        print(f"   {mark:9} {m:7} {p}")

    print("\n--- no-token sweep (expect 401 on non-open) ---")
    bad = []
    with TestClient(app, raise_server_exceptions=False) as c:
        for m, p in pairs:
            if is_open(p):
                continue
            req = re.sub(r"\{[^}]+\}", "dummy", p)
            try:
                if m == "GET":
                    r = c.get(req)
                elif m == "POST":
                    r = c.post(req, json={})
                elif m == "DELETE":
                    r = c.delete(req)
                elif m == "PUT":
                    r = c.put(req, json={})
                elif m == "PATCH":
                    r = c.patch(req, json={})
                else:
                    continue
            except Exception as e:  # noqa: BLE001
                print("   EXC", m, req, e)
                continue
            tag = "OK" if r.status_code == 401 else "**NOT401**"
            if r.status_code != 401:
                bad.append((m, req, r.status_code))
            print(f"   {tag:11} {m:7} {req:42} -> {r.status_code}")
    print("NON-401 routes:", bad)
