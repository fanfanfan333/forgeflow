"""FF-R1 P1 (FF-G5) — an MCP write approval is bound to its requester.

The snapshot's ``find_approved_write`` received the actor but never
compared it with the row's ``requester`` (FF-G5): a write approved
for alice could be spent by bob in the same tenant. It also read a
single ``limit=200`` page, so a valid approval older than 200 newer
rows was invisible. And nothing stopped the *same* approval from
being spent again by a repeated or concurrent resume (the 11-04
report's replay risk).

The contract pinned here:

* lookup matches tenant + requester + run + tool + payload digest —
  alice's approval answers alice's lookup and nobody else's;
* authorising consumes the approval atomically
  (``approved`` → ``consumed``): a replay is refused, and of two
  concurrent authorisations exactly one mints a ticket;
* the lookup walks past the first page of approved rows.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from forgeflow.config import get_settings
from forgeflow.governance.models import ApprovalRecord
from forgeflow.mcp import approvals as mcp_approvals
from forgeflow.mcp.approvals import (
    APPROVAL_KEY_PREFIX,
    WriteApprovalRequired,
    args_digest,
    authorize_write,
    find_approved_write,
)
from forgeflow.repositories import get_policy_repository
from forgeflow.repositories.memory.policy_repo import clear_policy_store

pytestmark = pytest.mark.asyncio

TOOL = "invoice_post"
ARGS = {"payload": 1}


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
            TOOL: {
                "read_only": False,
                "approval": "per_call",
                "tenants": ["t1"],
                "roles": ["manager"],
            }
        },
    )
    monkeypatch.setattr(mcp_approvals, "LEDGER", mcp_approvals.TicketLedger())
    mcp_approvals.clear_audit()
    return settings


@pytest.fixture
def clean_approvals():
    clear_policy_store()
    yield
    clear_policy_store()


def _approved_row(
    *,
    requester: str = "alice",
    run_id: str = "run-1",
    tool: str = TOOL,
    digest: str | None = None,
    created_at: datetime | None = None,
    resolved_at: datetime | None = None,
) -> ApprovalRecord:
    now = datetime.now(UTC)
    return ApprovalRecord(
        tenant_id="t1",
        run_id=run_id,
        risk_level="high",
        requested_action=f"{APPROVAL_KEY_PREFIX}{tool}",
        requester=requester,
        approver="operator",
        decision="approve",
        status="approved",
        created_at=created_at or now,
        resolved_at=resolved_at or now,
        payload_digest=args_digest(ARGS) if digest is None else digest,
    )


async def _save(row: ApprovalRecord) -> ApprovalRecord:
    return await get_policy_repository().save_approval(row)


# --------------------------------------------------------------------------- #
# FF-G5 acceptance: alice's approval is alice's                                #
# --------------------------------------------------------------------------- #


async def test_alice_approval_is_invisible_to_bob(secured, clean_approvals):
    row = await _save(_approved_row(requester="alice"))
    digest = args_digest(ARGS)
    assert await find_approved_write("t1", "bob", TOOL, digest, run_id="run-1") is None
    found = await find_approved_write("t1", "alice", TOOL, digest, run_id="run-1")
    assert found is not None and found.id == row.id


async def test_approval_does_not_cross_runs(secured, clean_approvals):
    await _save(_approved_row(requester="alice", run_id="run-1"))
    digest = args_digest(ARGS)
    assert await find_approved_write("t1", "alice", TOOL, digest, run_id="run-2") is None
    assert await find_approved_write("t1", "alice", TOOL, digest, run_id="run-1") is not None


async def test_bob_cannot_spend_alices_approval(secured, clean_approvals):
    await _save(_approved_row(requester="alice"))
    with pytest.raises(WriteApprovalRequired):
        await authorize_write(
            tenant="t1",
            actor="bob",
            role="manager",
            run_id="run-1",
            tool=TOOL,
            arguments=ARGS,
        )
    # Bob's failed attempt must not consume alice's approval either.
    ticket = await authorize_write(
        tenant="t1",
        actor="alice",
        role="manager",
        run_id="run-1",
        tool=TOOL,
        arguments=ARGS,
    )
    assert ticket.tool == TOOL


# --------------------------------------------------------------------------- #
# Single use: replay and concurrent resume                                     #
# --------------------------------------------------------------------------- #


async def test_approval_cannot_be_spent_twice(secured, clean_approvals):
    row = await _save(_approved_row(requester="alice"))
    await authorize_write(
        tenant="t1", actor="alice", role="manager", run_id="run-1", tool=TOOL, arguments=ARGS
    )
    stored = await get_policy_repository().get_approval("t1", row.id)
    assert stored is not None and stored.status == "consumed"
    with pytest.raises(WriteApprovalRequired):
        await authorize_write(
            tenant="t1",
            actor="alice",
            role="manager",
            run_id="run-1",
            tool=TOOL,
            arguments=ARGS,
        )


async def test_concurrent_authorisations_only_one_wins(secured, clean_approvals):
    await _save(_approved_row(requester="alice"))

    async def _attempt():
        return await authorize_write(
            tenant="t1",
            actor="alice",
            role="manager",
            run_id="run-1",
            tool=TOOL,
            arguments=ARGS,
        )

    results = await asyncio.gather(_attempt(), _attempt(), return_exceptions=True)
    tickets = [r for r in results if not isinstance(r, Exception)]
    refusals = [r for r in results if isinstance(r, WriteApprovalRequired)]
    assert len(tickets) == 1, results
    assert len(refusals) == 1, results


# --------------------------------------------------------------------------- #
# The lookup is not capped at the first 200 approved rows                      #
# --------------------------------------------------------------------------- #


async def test_approval_beyond_the_first_page_is_found(secured, clean_approvals):
    now = datetime.now(UTC)
    target = await _save(
        _approved_row(
            requester="alice",
            created_at=now - timedelta(hours=2),
            resolved_at=now,
        )
    )
    repo = get_policy_repository()
    for index in range(250):
        await repo.save_approval(
            _approved_row(
                requester="alice",
                tool=f"other_tool_{index}",
                created_at=now - timedelta(minutes=1),
                resolved_at=now,
            )
        )
    found = await find_approved_write("t1", "alice", TOOL, args_digest(ARGS), run_id="run-1")
    assert found is not None and found.id == target.id
