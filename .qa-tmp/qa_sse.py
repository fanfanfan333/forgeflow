import os, sys
sys.path.insert(0, r"D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台\ForgeFlow-main")
os.environ["STORAGE_BACKEND"] = "memory"
os.environ["LLM_PROVIDER"] = "mock"
os.environ["EMBEDDING_PROVIDER"] = "mock"
os.environ["API_SECRET_KEY"] = "qa-secret-key-1234567890"
os.environ["DEV_LOGIN_ENABLED"] = "true"
os.environ["DEV_LOGIN_PASSWORD"] = "qa-dev"
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

with patch("forgeflow.api.main.init_pool", new_callable=AsyncMock) as mp, \
     patch("forgeflow.api.main.compile_graph", new_callable=AsyncMock) as mg, \
     patch("forgeflow.api.main.get_mcp_tools", new_callable=AsyncMock, return_value=[]):
    mp.return_value = MagicMock()
    mg.return_value = MagicMock()
    from forgeflow.api.main import app
    from forgeflow.auth.jwt import create_access_token
    h = {"Authorization": "Bearer " + create_access_token(user_id="admin", role="admin")}
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post("/tasks", json={"intent": "SSE 验证任务"}, headers=h)
        rid = r.json()["run_id"]
        print("run_id:", rid)
        with c.stream("GET", f"/runs/{rid}/events", headers=h) as s:
            print("status", s.status_code, "ct", s.headers.get("content-type"),
                  "x-accel", s.headers.get("x-accel-buffering"))
            body = "".join(s.iter_text())
        print("has_data_frames:", body.count("data:") )
        print("has_DONE:", "[DONE]" in body)
        print("types:", [ln for ln in body.splitlines() if ln.startswith("data:")][:8])
        print("--- tail ---")
        print(body[-300:])
