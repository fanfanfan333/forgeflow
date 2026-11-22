"""BIZ-15 — write tools on the MCP surface, gated per call.

The reported gap was "MCP 授权只允许只读工具": ``approved_grant`` demanded
``read_only is True``, so a deployment could not authorise a write tool at all.
Relaxing that check alone would have been a downgrade — one session token would
authorise unlimited writes for its 120 s lifetime. These tests pin the
replacement contract:

* a write grant must declare ``approval: "per_call"`` (a bare
  ``read_only: false`` authorises nothing),
* the call needs a ticket minted from an approved ``mcp.write:<tool>`` decision,
* the ticket is payload-bound (arguments digest), single-use and short-lived,
* a blocked attempt raises *before* dispatch, so nothing can be reported as done,
* what cannot be done says so: no approval ⇒ a pending request for a human.

The HTTP tests drive the real FastMCP server + middleware, so the assertions are
about wire behaviour (a JSON-RPC error and an untouched tool), not about
internal calls.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fastmcp import FastMCP
from pydantic import SecretStr
from starlette.testclient import TestClient

from forgeflow.config import Settings, get_settings
from forgeflow.governance.models import ApprovalRecord
from forgeflow.mcp import approvals as mcp_approvals
from forgeflow.mcp.approvals import (
    ALREADY_USED,
    APPROVAL_KEY_PREFIX,
    ARGUMENT_MISMATCH,
    EXPIRED,
    MISSING,
    TOOL_MISMATCH,
    TicketLedger,
    WriteApprovalRequired,
    args_digest,
    authorize_write,
    mint_ticket,
)
from forgeflow.mcp.auth import (
    MCPAuthorization,
    MCPTokenVerifier,
    approved_grant,
    issue_token,
)

WRITE_TOOL = "invoice_post"
OTHER_WRITE = "invoice_void"
READ_TOOL = "invoice_read"


@pytest.fixture
def secured(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(
        settings, "mcp_service_secret", SecretStr("mcp-test-separate-key-" + "x" * 32)
    )
    monkeypatch.setattr(
        settings,
        "mcp_tool_grants",
        {
            READ_TOOL: {"read_only": True, "tenants": ["t1"], "roles": ["manager"]},
            WRITE_TOOL: {
                "read_only": False,
                "approval": "per_call",
                "tenants": ["t1"],
                "roles": ["manager"],
            },
            OTHER_WRITE: {
                "read_only": False,
                "approval": "per_call",
                "tenants": ["t1"],
                "roles": ["manager"],
            },
            # A copy-pasted write grant without the policy: authorises nothing.
            "bare_write": {"read_only": False, "tenants": ["t1"], "roles": ["manager"]},
        },
    )
    monkeypatch.setattr(mcp_approvals, "LEDGER", TicketLedger())
    mcp_approvals.clear_audit()
    return settings


def _approved(tenant: str, tool: str, *, age_seconds: float = 0.0) -> ApprovalRecord:
    """An approval row shaped exactly like the one the approvals API writes."""
    from datetime import UTC, datetime

    resolved = datetime.fromtimestamp(time.time() - age_seconds, UTC)
    return ApprovalRecord(
        tenant_id=tenant,
        run_id="run-1",
        risk_level="high",
        requested_action=f"{APPROVAL_KEY_PREFIX}{tool}",
        requester="agent",
        approver="operator",
        decision="approve",
        status="approved",
        resolved_at=resolved,
    )


# --------------------------------------------------------------------------- #
# Grant policy                                                                #
# --------------------------------------------------------------------------- #
def test_write_grant_requires_the_per_call_policy(secured):
    assert approved_grant(READ_TOOL, "t1", "manager") is not None
    assert approved_grant(WRITE_TOOL, "t1", "manager") is not None
    # No declared write policy ⇒ no grant, even though read_only is a real bool.
    assert approved_grant("bare_write", "t1", "manager") is None
    # The old limits still hold.
    assert approved_grant(WRITE_TOOL, "t2", "manager") is None
    assert approved_grant(WRITE_TOOL, "t1", "viewer") is None


def test_unknown_write_policy_refuses_to_boot():
    with pytest.raises(ValueError, match="per_call"):
        Settings.parse_mcp_grants(
            {
                "invoice_post": {
                    "read_only": False,
                    "approval": "sometimes",
                    "tenants": ["t1"],
                    "roles": ["manager"],
                }
            }
        )


def test_registry_installs_only_usable_write_grants(secured):
    """The runtime registry must not expose a write tool it cannot authorise."""
    from forgeflow.mcp.client import registry

    definition = SimpleNamespace(
        name=WRITE_TOOL,
        description="post an invoice",
        input_schema={"type": "object", "properties": {"payload": {"type": "integer"}}},
    )
    bare = SimpleNamespace(
        name="bare_write",
        description="write with no policy",
        input_schema={"type": "object", "properties": {}},
    )
    try:
        registry.install(definition, "http://mcp.invalid")
        assert registry.schema(registry.tool_id(WRITE_TOOL)) is not None
        registry.install(bare, "http://mcp.invalid")
        assert registry.schema(registry.tool_id("bare_write")) is None
    finally:
        registry.clear()


# --------------------------------------------------------------------------- #
# Ticket shape and ledger                                                     #
# --------------------------------------------------------------------------- #
def test_token_refuses_a_malformed_or_outliving_ticket(secured):
    good = mint_ticket(
        WRITE_TOOL, {"id": 1}, approval_id="a1", tenant="t1", actor="u1", run_id="run-1"
    ).to_claim()
    token = issue_token("t1", "u1", "manager", [WRITE_TOOL], [good])
    assert token

    broken = [dict(good, digest="not-a-digest")]
    assert _verifiable(_token("t1", "u1", "manager", broken)) is False

    outlives = [dict(good, exp=good["exp"] + 600)]
    assert _verifiable(_token("t1", "u1", "manager", outlives)) is False

    too_many = [good] * (mcp_approvals.MAX_TICKETS_PER_TOKEN + 1)
    with pytest.raises(ValueError, match="at most"):
        _token("t1", "u1", "manager", too_many)
    # …and a hand-crafted token with the same surplus is rejected on the wire.
    assert _verifiable(_forged(secured, too_many)) is False


def _forged(settings, tickets: list[dict]) -> str:
    """A token signed with the right key but claims our issuer would never mint."""
    import jwt

    now = int(time.time())
    return jwt.encode(
        {
            "iss": "forgeflow-mcp-client",
            "aud": "forgeflow-mcp",
            "sub": "u1",
            "tenant": "t1",
            "role": "manager",
            "tools": [WRITE_TOOL],
            "wticks": tickets,
            "iat": now,
            "nbf": now,
            "exp": now + 120,
            "jti": "forged",
        },
        settings.mcp_service_secret.get_secret_value(),
        algorithm="HS256",
    )


def _token(tenant: str, actor: str, role: str, tickets: list[dict]) -> str:
    return issue_token(tenant, actor, role, [WRITE_TOOL], tickets)


def _verifiable(token: str) -> bool:
    import asyncio

    return asyncio.run(MCPTokenVerifier().verify_token(token)) is not None


def test_ledger_names_every_rejection_reason(secured):
    ledger = mcp_approvals.LEDGER
    digest = args_digest({"id": 1})
    ticket = mint_ticket(
        WRITE_TOOL, {"id": 1}, approval_id="a1", tenant="t1", actor="u1", run_id="run-1"
    ).to_claim()

    assert ledger.consume(None, tool=WRITE_TOOL, digest=digest).reason == MISSING
    assert ledger.consume(ticket, tool=OTHER_WRITE, digest=digest).reason == TOOL_MISMATCH
    assert ledger.consume(ticket, tool=WRITE_TOOL, digest=args_digest({"id": 2})).reason == (
        ARGUMENT_MISMATCH
    )
    assert ledger.consume(ticket, tool=WRITE_TOOL, digest=digest).ok is True
    assert ledger.consume(ticket, tool=WRITE_TOOL, digest=digest).reason == ALREADY_USED
    stale = dict(ticket, id="stale", exp=int(time.time()) - 1)
    assert ledger.consume(stale, tool=WRITE_TOOL, digest=digest).reason == EXPIRED


# --------------------------------------------------------------------------- #
# Real transport: nothing runs without a ticket                               #
# --------------------------------------------------------------------------- #
def _server() -> tuple[FastMCP, list[dict]]:
    server = FastMCP("write-gated", auth=MCPTokenVerifier(), middleware=[MCPAuthorization()])
    calls: list[dict] = []

    @server.tool()
    async def invoice_post(payload: int) -> dict:
        calls.append({"payload": payload})
        return {"posted": payload}

    return server, calls


def _client(server: FastMCP) -> TestClient:
    # ``raise_server_exceptions=False``: a middleware refusal is not an
    # in-tool error, so FastMCP may surface it as a 500 instead of a JSON-RPC
    # error object. Both are "the call failed"; what matters is the tool ran 0
    # times, and the test asserts exactly that.
    return TestClient(
        server.http_app(json_response=True, stateless_http=True), raise_server_exceptions=False
    )


def _call(client: TestClient, token: str, arguments: dict) -> dict:
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": WRITE_TOOL, "arguments": arguments},
        },
        headers={
            "accept": "application/json, text/event-stream",
            "authorization": "Bearer " + token,
        },
    )
    body: dict = {}
    if response.headers.get("content-type", "").startswith("application/json"):
        body = response.json()
    return {"status": response.status_code, "body": body}


def _failed(result: dict) -> bool:
    if result["status"] != 200:
        return True
    body = result["body"]
    return "error" in body or body.get("result", {}).get("isError") is True


def test_write_without_ticket_is_refused_and_never_reaches_the_tool(secured):
    server, calls = _server()
    with _client(server) as client:
        result = _call(client, issue_token("t1", "u1", "manager", [WRITE_TOOL]), {"payload": 1})
        assert _failed(result)
        assert calls == []
    assert [entry["outcome"] for entry in mcp_approvals.recent_audit()] == [MISSING]


def test_ticket_authorises_one_payload_bound_call(secured):
    server, calls = _server()
    ticket = mint_ticket(
        WRITE_TOOL, {"payload": 1}, approval_id="a1", tenant="t1", actor="u1", run_id="run-1"
    )
    token = issue_token("t1", "u1", "manager", [WRITE_TOOL], [ticket.to_claim()])
    with _client(server) as client:
        assert not _failed(_call(client, token, {"payload": 1}))
        assert calls == [{"payload": 1}]

        # Replay of the same token: the ticket is burnt.
        assert _failed(_call(client, token, {"payload": 1}))
        assert calls == [{"payload": 1}]

        # Payload swap with a *fresh* ticket minted for different arguments.
        other = mint_ticket(
            WRITE_TOOL, {"payload": 1}, approval_id="a1", tenant="t1", actor="u1", run_id="run-1"
        )
        swapped = issue_token("t1", "u1", "manager", [WRITE_TOOL], [other.to_claim()])
        assert _failed(_call(client, swapped, {"payload": 2}))
        assert calls == [{"payload": 1}]

    outcomes = [entry["outcome"] for entry in mcp_approvals.recent_audit()]
    # One mint per ticket, one burn on the accepted call, and the two refusals.
    assert outcomes.count("minted") == 2
    assert outcomes.count("consumed") == 1
    assert ALREADY_USED in outcomes
    assert ARGUMENT_MISMATCH in outcomes


def test_read_tool_needs_no_ticket(secured):
    server = FastMCP("read-only", auth=MCPTokenVerifier(), middleware=[MCPAuthorization()])
    seen: list[int] = []

    @server.tool()
    async def invoice_read() -> dict:
        seen.append(1)
        return {"ok": True}

    with TestClient(server.http_app(json_response=True, stateless_http=True)) as client:
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": READ_TOOL, "arguments": {}},
            },
            headers={
                "accept": "application/json, text/event-stream",
                "authorization": "Bearer " + issue_token("t1", "u1", "manager", [READ_TOOL]),
            },
        )
    assert response.status_code == 200
    assert seen == [1]


# --------------------------------------------------------------------------- #
# Control-plane side: the approval has to exist, and be fresh                  #
# --------------------------------------------------------------------------- #
@pytest.fixture
def clean_approvals():
    from forgeflow.repositories.memory.policy_repo import clear_policy_store

    clear_policy_store()
    yield
    clear_policy_store()


async def test_write_needs_an_approved_decision_and_asks_for_one(secured, clean_approvals):
    with pytest.raises(WriteApprovalRequired, match="approved request"):
        await authorize_write(
            tenant="t1",
            actor="u1",
            role="manager",
            run_id="run-1",
            tool=WRITE_TOOL,
            arguments={"payload": 1},
        )
    from forgeflow.repositories import get_policy_repository

    pending = await get_policy_repository().list_approvals("t1", status="pending")
    assert [row.requested_action for row in pending] == [f"{APPROVAL_KEY_PREFIX}{WRITE_TOOL}"]
    assert pending[0].requester == "u1"

    # The agent retrying must not flood the approval centre.
    with pytest.raises(WriteApprovalRequired):
        await authorize_write(
            tenant="t1",
            actor="u1",
            role="manager",
            run_id="run-1",
            tool=WRITE_TOOL,
            arguments={"payload": 1},
        )
    assert len(await get_policy_repository().list_approvals("t1", status="pending")) == 1

    approved = _approved("t1", WRITE_TOOL)
    await get_policy_repository().save_approval(approved)
    ticket = await authorize_write(
        tenant="t1",
        actor="u1",
        role="manager",
        run_id="run-2",
        tool=WRITE_TOOL,
        arguments={"payload": 1},
    )
    assert ticket.approval_id == approved.id
    assert ticket.digest == args_digest({"payload": 1})


async def test_other_tool_other_tenant_and_stale_approvals_do_not_authorise(
    secured, clean_approvals
):
    from forgeflow.repositories import get_policy_repository

    repo = get_policy_repository()
    await repo.save_approval(_approved("t1", OTHER_WRITE))
    await repo.save_approval(_approved("t2", WRITE_TOOL))
    await repo.save_approval(
        _approved("t1", WRITE_TOOL, age_seconds=secured.mcp_write_approval_window_seconds + 60)
    )

    with pytest.raises(WriteApprovalRequired):
        await authorize_write(
            tenant="t1",
            actor="u1",
            role="manager",
            run_id="run-1",
            tool=WRITE_TOOL,
            arguments={},
        )
    outcomes = [entry["outcome"] for entry in mcp_approvals.recent_audit()]
    assert mcp_approvals.NO_APPROVAL in outcomes


async def test_ungranted_identity_cannot_mint_a_ticket(secured, clean_approvals):
    from forgeflow.repositories import get_policy_repository

    await get_policy_repository().save_approval(_approved("t1", WRITE_TOOL))
    with pytest.raises(WriteApprovalRequired, match="not granted"):
        await authorize_write(
            tenant="t1",
            actor="u1",
            role="viewer",
            run_id="run-1",
            tool=WRITE_TOOL,
            arguments={},
        )
    # An approval alone must not upgrade a read tool into a write.
    with pytest.raises(WriteApprovalRequired):
        await authorize_write(
            tenant="t1",
            actor="u1",
            role="manager",
            run_id="run-1",
            tool=READ_TOOL,
            arguments={},
        )


# --------------------------------------------------------------------------- #
# Registry handler: refusal happens before dispatch                           #
# --------------------------------------------------------------------------- #
async def test_registry_handler_refuses_before_calling_the_server(
    secured, clean_approvals, monkeypatch
):
    from forgeflow.mcp.client import registry, sessions
    from forgeflow.runtime.tool_registry import resolve

    dispatched: list[tuple] = []

    async def fake_call_tool(*args, **kwargs):
        dispatched.append((args, kwargs))
        return SimpleNamespace(is_error=False, structured_content={"ok": True}, content=[])

    monkeypatch.setattr(sessions, "call_tool", fake_call_tool)
    definition = SimpleNamespace(
        name=WRITE_TOOL,
        description="post an invoice",
        input_schema={"type": "object", "properties": {"payload": {"type": "integer"}}},
    )
    registry.install(definition, "http://mcp.invalid")
    try:
        binding = resolve(registry.tool_id(WRITE_TOOL))
        assert binding is not None
        ctx = SimpleNamespace(
            tenant_id="t1", user_id="u1", role="manager", run_id="run-1", task_context=None
        )
        with pytest.raises(WriteApprovalRequired):
            await binding.handler({"payload": 1}, ctx)
        assert dispatched == []

        from forgeflow.repositories import get_policy_repository

        await get_policy_repository().save_approval(_approved("t1", WRITE_TOOL))
        payload = await binding.handler({"payload": 1}, ctx)
        assert payload["mcp"]["write"] is True
        assert payload["mcp"]["approval_id"]
        assert dispatched and dispatched[0][1]["write_ticket"] is not None
    finally:
        registry.clear()
