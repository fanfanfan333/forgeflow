import os, sys, traceback
sys.path.insert(0, r"D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台\ForgeFlow-main")
os.environ["STORAGE_BACKEND"] = "memory"
os.environ["LLM_PROVIDER"] = "mock"
os.environ["EMBEDDING_PROVIDER"] = "mock"
os.environ["API_SECRET_KEY"] = "qa-secret-key-1234567890"
os.environ["DEV_LOGIN_ENABLED"] = "true"
os.environ["DEV_LOGIN_PASSWORD"] = "qa-dev"
os.environ["OTEL_ENVIRONMENT"] = "development"

from fastapi.testclient import TestClient
from forgeflow.api.main import app

try:
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/health")
        print("STARTUP_OK", r.status_code, r.json())
except Exception as e:  # noqa: BLE001
    print("STARTUP_FAILED:", type(e).__name__, str(e)[:400])
    traceback.print_exc()
