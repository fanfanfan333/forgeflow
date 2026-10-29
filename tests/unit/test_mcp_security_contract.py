"""Actual MCP framing plus identity and structured-result regression."""

from types import SimpleNamespace

import jwt
import pytest
from fastmcp import FastMCP
from pydantic import SecretStr
from starlette.testclient import TestClient

from forgeflow.config import get_settings
from forgeflow.mcp.auth import (
    ISSUER,
    MCPAuthorization,
    MCPTokenVerifier,
    approved_grant,
    issue_token,
)
from forgeflow.mcp.client.adapter import result_data


@pytest.fixture
def secured(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(
        settings, "mcp_service_secret", SecretStr("mcp-test-separate-key-" + "x" * 32)
    )
    monkeypatch.setattr(
        settings,
        "mcp_tool_grants",
        {"read": {"read_only": True, "tenants": ["a"], "roles": ["manager"]}},
    )
    return settings


@pytest.mark.asyncio
async def test_mcp_credentials_have_separate_audience_and_expiry(secured):
    token = issue_token("a", "reader", "manager", ["read"])
    verified = await MCPTokenVerifier().verify_token(token)
    assert verified.claims["tenant"] == "a"
    assert verified.claims["exp"] - verified.claims["iat"] == 120
    wrong = jwt.encode(
        {"iss": ISSUER, "aud": "forgeflow-api", "sub": "reader"},
        secured.mcp_service_secret.get_secret_value(),
        algorithm="HS256",
    )
    assert await MCPTokenVerifier().verify_token(wrong) is None


def test_missing_grant_and_cross_tenant_deny(secured):
    assert approved_grant("read", "a", "manager")
    assert approved_grant("read", "b", "manager") is None
    assert approved_grant("read", "a", "sales_rep") is None
    assert approved_grant("write", "a", "admin") is None


@pytest.mark.parametrize("grant", [
    {"read_only": True, "tenants": "tenant-a", "roles": ["manager"]},
    {"read_only": True, "tenants": ["a"], "roles": "manager"},
    {"read_only": "true", "tenants": ["a"], "roles": ["manager"]},
])
def test_grant_configuration_rejects_substring_scope_and_untyped_risk(grant):
    from forgeflow.config import Settings
    with pytest.raises(ValueError):
        Settings.parse_mcp_grants({"read": grant})


@pytest.mark.parametrize("payload", [{"ok": False}, {"error": "rejected"}])
def test_business_failure_is_not_success(payload):
    with pytest.raises(RuntimeError, match="business"):
        result_data(SimpleNamespace(is_error=False, structured_content=payload))


def test_protocol_failure_is_not_success():
    with pytest.raises(RuntimeError, match="tool error"):
        result_data(SimpleNamespace(is_error=True))


def test_actual_http_transport_rejects_anonymous_and_api_tokens(secured):
    server = FastMCP("secured", auth=MCPTokenVerifier(), middleware=[MCPAuthorization()])
    with TestClient(server.http_app()) as client:
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        }
        headers = {"accept": "application/json, text/event-stream"}
        assert client.post("/mcp", json=request, headers=headers).status_code == 401
        headers["authorization"] = "Bearer invalid"
        assert client.post("/mcp", json=request, headers=headers).status_code == 401
        headers["authorization"] = "Bearer " + issue_token("a", "reader", "manager", ["read"])
        assert client.post("/mcp", json=request, headers=headers).status_code == 200


def test_shared_api_signing_key_is_refused(secured, monkeypatch):
    monkeypatch.setattr(secured, "mcp_service_secret", secured.api_secret_key)
    with pytest.raises(ValueError, match="separate"):
        issue_token("a", "reader", "manager", ["read"])


def test_actual_http_call_enforces_tenant_role_and_tool_scope(secured):
    from forgeflow.tenant_scope import current_tenant

    server = FastMCP("tenant-bound", auth=MCPTokenVerifier(), middleware=[MCPAuthorization()])
    calls = []

    @server.tool()
    async def read() -> dict:
        calls.append(current_tenant())
        return {"tenant": current_tenant(), "value": 7}

    with TestClient(server.http_app(json_response=True, stateless_http=True)) as client:
        for tenant, role, tools, permitted in [
            ("a", "manager", ["read"], True),
            ("b", "manager", ["read"], False),
            ("a", "sales_rep", ["read"], False),
            ("a", "manager", [], False),
        ]:
            response = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "read", "arguments": {}},
                },
                headers={
                    "accept": "application/json, text/event-stream",
                    "authorization": "Bearer " + issue_token(tenant, "reader", role, tools),
                },
            )
            assert response.status_code == 200
            result = response.json()
            if permitted:
                assert result["result"]["structuredContent"]["tenant"] == "a"
            else:
                assert "error" in result or result.get("result", {}).get("isError") is True
    assert calls == ["a"]
