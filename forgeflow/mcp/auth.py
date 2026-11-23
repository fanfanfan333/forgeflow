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
from forgeflow.mcp import approvals as mcp_approvals
from forgeflow.mcp.approvals import MAX_TICKETS_PER_TOKEN, WriteTicket
from forgeflow.tenant_scope import tenant_scope

ISSUER = "forgeflow-mcp-client"
AUDIENCE = "forgeflow-mcp"
#: The only write policy an MCP grant may declare. Named (rather than inlined)
#: because the config validator, the middleware and the docs must agree.
WRITE_APPROVAL_PER_CALL = "per_call"
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


def issue_token(
    tenant: str,
    actor: str,
    role: str,
    tools: list[str],
    write_tickets: list[dict[str, Any]] | None = None,
) -> str:
    """Mint a short-lived MCP token.

    ``write_tickets`` carries at most :data:`~forgeflow.mcp.approvals.MAX_TICKETS_PER_TOKEN`
    single-use authorisations, each bound to one tool + argument digest + approval
    row. A token without tickets can only ever *read*: the middleware refuses
    every write, so a forgotten ticket fails closed instead of open.
    """
    now = int(time.time())
    claims: dict[str, Any] = {
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
    }
    tickets = [ticket for ticket in (write_tickets or []) if isinstance(ticket, dict)]
    if len(tickets) > MAX_TICKETS_PER_TOKEN:
        # Silently dropping the surplus would let a caller believe five writes
        # were approved while only four can run. Caller error ⇒ raise.
        raise ValueError(f"at most {MAX_TICKETS_PER_TOKEN} write tickets per token")
    if tickets:
        claims["wticks"] = tickets
    return jwt.encode(claims, service_key(), algorithm="HS256")


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
            # Write tickets are optional, but a malformed one invalidates the
            # whole token: silently dropping it would turn "badly approved
            # write" into "unapproved read-only token" and hide the bug.
            tickets = claims.get("wticks", [])
            if not isinstance(tickets, list) or len(tickets) > MAX_TICKETS_PER_TOKEN:
                return None
            if any(WriteTicket.from_claim(ticket) is None for ticket in tickets):
                return None
            if any(ticket["exp"] > claims["exp"] for ticket in tickets):
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
    # A write grant must *declare* the strongest approval policy. A bare
    # ``read_only: false`` (e.g. copied from a docs snippet) authorises nothing:
    # deny-by-default is the only safe reading of a missing policy.
    if grant["read_only"] is False and grant.get("approval") != WRITE_APPROVAL_PER_CALL:
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
            # A write tool is *discoverable* once the deployment granted it (that
            # is the deployment-level approval); actually calling it still needs
            # a per-call ticket, checked in ``on_call_tool``. Hiding it here while
            # allowing the call would make discovery lie in the safe direction
            # and leave the planner unable to ever ask for approval.
            and (grant["read_only"] is True or grant.get("approval") == WRITE_APPROVAL_PER_CALL)
        ]

    async def on_call_tool(self, context: Any, call_next: Any):
        token = get_access_token()
        claims = token.claims if token else {}
        name = context.message.name
        tenant = claims.get("tenant", "")
        actor = claims.get("sub", "")
        grant = approved_grant(name, tenant, claims.get("role", ""))
        if (
            not claims.get("tenant")
            or not claims.get("sub")
            or name not in claims.get("tools", [])
            or grant is None
        ):
            raise PermissionError("MCP tool is not authorized for this identity")
        arguments = context.message.arguments or {}
        if any(key in arguments for key in ("tenant", "tenant_id", "actor", "user_id", "role")):
            raise PermissionError("MCP identity arguments are server-owned")
        if grant["read_only"] is not True:
            # Write: burn a per-call ticket *before* the tool runs. A rejection
            # here is the "no side effect happened" guarantee — nothing has been
            # dispatched at this point.
            verdict = self._consume_ticket(claims, name, arguments)
            if not verdict.ok:
                mcp_approvals.record(
                    "dispatch",
                    verdict.reason,
                    tenant=tenant,
                    actor=actor,
                    tool=name,
                    digest=mcp_approvals.args_digest(arguments),
                    approval_id=verdict.approval_id,
                    detail=verdict.ticket_id,
                )
                raise PermissionError(
                    f"MCP write tool '{name}' requires a fresh per-call approval ({verdict.reason})"
                )
        with (
            tenant_scope(claims["tenant"]),
            mcp_identity(claims["tenant"], claims["sub"], claims["role"]),
        ):
            return await call_next(context)

    @staticmethod
    def _consume_ticket(claims: dict, name: str, arguments: Any) -> mcp_approvals.TicketVerdict:
        """Burn the one ticket that was minted for *this* tool, or say why not.

        Tickets for other tools in the same token are skipped (they belong to
        another call); among the ones for this tool the first still-matching
        ticket wins. Otherwise the first real reason — expired, wrong payload,
        already used — is returned so the caller can tell an operator *which*
        step failed instead of a generic "denied".
        """
        digest = mcp_approvals.args_digest(arguments)
        mine: list[dict] = []
        for ticket in claims.get("wticks", []) or []:
            parsed = WriteTicket.from_claim(ticket)
            if parsed is not None and parsed.tool == name:
                mine.append(ticket)
        if not mine:
            return mcp_approvals.LEDGER.consume(None, tool=name, digest=digest)
        first = None
        for ticket in mine:
            verdict = mcp_approvals.LEDGER.consume(ticket, tool=name, digest=digest)
            if verdict.ok:
                mcp_approvals.record(
                    "dispatch",
                    "consumed",
                    tenant=str(claims.get("tenant", "")),
                    actor=str(claims.get("sub", "")),
                    tool=name,
                    digest=digest,
                    approval_id=verdict.approval_id,
                    detail=verdict.ticket_id,
                )
                return verdict
            first = first or verdict
        if first is not None:
            return first
        return mcp_approvals.LEDGER.consume(None, tool=name, digest=digest)
