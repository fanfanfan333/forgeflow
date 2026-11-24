"""FF-R1 P4 — delivery guards for the email write tool.

BIZ-13 (test_biz13_real_email_delivery) makes the send itself honest:
real SMTP or an explicit ``not_configured`` / ``failed`` — never a
pretend "sent". This file pins the two platform guards the P4 brief
requires around that send, reusing the FF-R1 P1 (FF-G5) approval
machinery with the email tool's registered name
(``email_send_email`` — namespace ``email`` + function ``send_email``):

* **Approval gate**: with a ``per_call`` write grant, no approval row
  means ``authorize_write`` refuses — the write cannot run at all.
* **Idempotency**: one approval authorises exactly one send. The
  first authorisation consumes the approval atomically; a retry or
  a resume replaying the same call is refused, so the mail cannot
  be sent twice off one approval.
"""

from __future__ import annotations

from datetime import UTC, datetime

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
)
from forgeflow.repositories import get_policy_repository
from forgeflow.repositories.memory.policy_repo import clear_policy_store

pytestmark = pytest.mark.asyncio

TOOL = "email_send_email"
ARGS = {"to": "buyer@example.com", "subject": "Proposal", "body": "See attached."}


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


def _approved_row(*, requester: str = "alice") -> ApprovalRecord:
    now = datetime.now(UTC)
    return ApprovalRecord(
        tenant_id="t1",
        run_id="run-1",
        risk_level="high",
        requested_action=f"{APPROVAL_KEY_PREFIX}{TOOL}",
        requester=requester,
        approver="operator",
        decision="approve",
        status="approved",
        created_at=now,
        resolved_at=now,
        payload_digest=args_digest(ARGS),
    )


async def test_send_email_without_approval_is_refused(secured, clean_approvals):
    with pytest.raises(WriteApprovalRequired):
        await authorize_write(
            tenant="t1",
            actor="alice",
            role="manager",
            run_id="run-1",
            tool=TOOL,
            arguments=ARGS,
        )


async def test_one_approval_sends_exactly_once_despite_retry(secured, clean_approvals):
    row = await get_policy_repository().save_approval(_approved_row())
    await authorize_write(
        tenant="t1", actor="alice", role="manager", run_id="run-1", tool=TOOL, arguments=ARGS
    )
    stored = await get_policy_repository().get_approval("t1", row.id)
    assert stored is not None and stored.status == "consumed"
    # A retry / resume replaying the same call finds nothing to spend.
    with pytest.raises(WriteApprovalRequired):
        await authorize_write(
            tenant="t1",
            actor="alice",
            role="manager",
            run_id="run-1",
            tool=TOOL,
            arguments=ARGS,
        )


async def test_another_user_cannot_spend_the_email_approval(secured, clean_approvals):
    await get_policy_repository().save_approval(_approved_row(requester="alice"))
    with pytest.raises(WriteApprovalRequired):
        await authorize_write(
            tenant="t1", actor="bob", role="manager", run_id="run-1", tool=TOOL, arguments=ARGS
        )
