"""Short-lived MCP credentials are distinct from user/API credentials."""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import jwt
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware

from forgeflow.config import get_settings
from forgeflow.tenant_scope import tenant_scope

ISSUER = "forgeflow-mcp-client"
AUDIENCE = "forgeflow-mcp"
identity: ContextVar[tuple[str, str, str] | None] = ContextVar("mcp_identity", default=None)
active_run: ContextVar[str] = ContextVar("mcp_run", default="")


@contextmanager
def mcp_identity(tenant: str, actor: str, role: str, run_id: str = ""):
    marker = identity.set((tenant, actor, role))
    run_marker = active_run.set(run_id)
    try:
        yield
    finally:
        identity.reset(marker)
        active_run.reset(run_marker)


def service_key() -> str:
    settings = get_settings()
    key = settings.mcp_service_secret.get_secret_value()
    if len(key) < 32 or key == settings.api_secret_key.get_secret_value():
        raise ValueError("MCP requires a separate signing key of at least 32 characters")
    return key


def issue_token(tenant: str, actor: str, role: str, tools: list[str]) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": actor,
            "tenant": tenant,
            "role": role,
            "tools": tools,
            "iat": now,
            "nbf": now,
            "exp": now + 120,
            "jti": uuid.uuid4().hex,
        },
        service_key(),
        algorithm="HS256",
    )


class MCPTokenVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = jwt.decode(
                token,
                service_key(),
                algorithms=["HS256"],
                issuer=ISSUER,
                audience=AUDIENCE,
                options={"require": ["sub", "tenant", "role", "tools", "exp", "iat", "nbf", "jti"]},
            )
            if not isinstance(claims["tools"], list) or any(
                not isinstance(t, str) for t in claims["tools"]
            ):
                return None
            if claims["exp"] - claims["iat"] > 120:
                return None
            if any(not isinstance(claims[key], str) for key in ("tenant", "role", "sub", "jti")):
                return None
            return AccessToken(
                token=token,
                client_id=claims["sub"],
                subject=claims["sub"],
                scopes=["mcp"],
                expires_at=claims["exp"],
                claims=claims,
            )
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            return None


def approved_grant(name: str, tenant: str, role: str) -> dict | None:
    settings = get_settings()
    if name.startswith(("crm_", "email_", "data_")) and not settings.allows_development_tools():
        return None
    grant = settings.mcp_tool_grants.get(name)
    # Missing risk classification never becomes a grant from server annotations.
    if not isinstance(grant, dict) or not isinstance(grant.get("read_only"), bool):
        return None
    if any(not isinstance(grant.get(field), list) for field in ("tenants", "roles")):
        return None
    if tenant not in grant.get("tenants", []) or role not in grant.get("roles", []):
        return None
    if name.startswith(
        (
            "hubspot_",
            "salesforce_",
            "jira_",
            "github_",
            "sap_",
            "quickbooks_",
            "msgraph_",
            "servicenow_",
            "slack_",
        )
    ) and (grant.get("credential_tenant") != tenant or grant.get("tenants") != [tenant]):
        return None
    return grant


class MCPAuthorization(Middleware):
    async def on_list_tools(self, context: Any, call_next: Any):
        token = get_access_token()
        if token is None:
            raise PermissionError("MCP discovery requires authentication")
        tools = await call_next(context)
        claims = token.claims
        if (
            claims.get("role") == "discovery"
            and claims.get("tenant") == ""
            and claims.get("tools") == []
        ):
            return tools
        return [
            tool
            for tool in tools
            if tool.name in claims.get("tools", [])
            and (
                grant := approved_grant(tool.name, claims.get("tenant", ""), claims.get("role", ""))
            )
            is not None
            and grant["read_only"] is True
        ]

    async def on_call_tool(self, context: Any, call_next: Any):
        token = get_access_token()
        claims = token.claims if token else {}
        name = context.message.name
        grant = approved_grant(name, claims.get("tenant", ""), claims.get("role", ""))
        if (
            not claims.get("tenant")
            or not claims.get("sub")
            or name not in claims.get("tools", [])
            or grant is None
            or grant["read_only"] is not True
        ):
            raise PermissionError("MCP tool is not authorized for this identity")
        if any(
            key in (context.message.arguments or {})
            for key in ("tenant", "tenant_id", "actor", "user_id", "role")
        ):
            raise PermissionError("MCP identity arguments are server-owned")
        with (
            tenant_scope(claims["tenant"]),
            mcp_identity(claims["tenant"], claims["sub"], claims["role"]),
        ):
            return await call_next(context)
