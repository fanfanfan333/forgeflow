from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.middleware.rate_limit import RateLimitMiddleware
from forgeflow.security import rate_limiter


@pytest.mark.parametrize(
    "app_env,telemetry_prod", [("prod", False), ("staging", False), ("dev", True)]
)
def test_either_production_environment_rejects_memory(monkeypatch, app_env, telemetry_prod):
    settings = Mock()
    settings.environment.return_value = app_env
    settings.is_production.return_value = telemetry_prod
    monkeypatch.setattr(rate_limiter, "get_settings", lambda: settings)
    with pytest.raises(RuntimeError, match="shared Redis"):
        rate_limiter.limiter.__wrapped__("memory://")


def test_unavailable_limit_storage_does_not_execute_request(monkeypatch):
    monkeypatch.setattr(
        rate_limiter, "consume", AsyncMock(side_effect=ConnectionError("Redis unavailable"))
    )
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware)
    executed = []

    @app.get("/protected")
    def protected():
        executed.append(True)
        return {"ok": True}

    response = TestClient(app).get("/protected")
    assert response.status_code == 503
    assert response.json() == {"error": "rate_limit_unavailable"}
    assert not executed
