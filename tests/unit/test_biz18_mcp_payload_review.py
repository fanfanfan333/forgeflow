"""BIZ-18 — an MCP write approval names the payload, and someone else decides.

Ported from the snapshot branch (``codex/skill-verification-20261017``,
commit 6a89ae0) for FF-R1 P1. One adaptation: ``_approved`` names the
test actor as requester, because main binds an approval to its
requester (FF-G5) where the snapshot only bound tenant + payload.
Everything else is verbatim.


BIZ-15 made a write call need an ``mcp.write:<tool>`` approval. That was still
two sizes too loose in ways this file pins down:

1. **The approval named a tool, not a payload.** ``requested_action`` was
   ``mcp.write:invoice_post`` and nothing else, so approving one invoice meant
   approving *any* ``invoice_post`` for the next 15 minutes. The approver never
   saw the arguments either — the inbox showed a tool name and a sentence.
2. **The requester was the only person who could see or decide it.**
   ``_visible_approval`` asked ``run.actor_user_id == user.user_id``, so the
   "human" in human-in-the-loop was the same human whose agent wanted to write.

The replacement contract, tested here:

* the pending row carries ``payload_digest`` (the ``sha256:`` of the canonical
  arguments) and a redacted, bounded preview in ``note``;
* an approval only authorises the payload it named — a different payload is
  ``no_approved_request`` at the gate, before any dispatch;
* a pre-043 row with an empty digest authorises nothing and says why
  (``legacy_approval``), instead of silently widening to a wildcard;
* pending requests are idempotent per ``(run, tool, digest)``, so a retry does
  not spam the inbox but a genuinely different payload gets its own decision;
* the requester cannot approve their own MCP write unless the deployment opts in
  explicitly, and a reviewer seat can see and decide it.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from starlette.testclient import TestClient

from forgeflow.governance.models import ApprovalRecord
from forgeflow.mcp import approvals as mcp_approvals

# Only names that exist on the pre-fix tree are imported here; everything the
# fix introduced is reached through the module, so a baseline revert fails on
# behaviour (a missing capability) instead of a collection-time ImportError.
from forgeflow.mcp.approvals import (
    APPROVAL_KEY_PREFIX,
    NO_APPROVAL,
    WriteApprovalRequired,
    args_digest,
    authorize_write,
    request_approval,
)
from forgeflow.repositories import get_policy_repository

WRITE_TOOL = "invoice_post"


@pytest.fixture
def secured(monkeypatch):
    from forgeflow.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(
        settings, "mcp_service_secret", SecretStr("mcp-test-separate-key-" + "x" * 32)
    )
    monkeypatch.setattr(
        settings,
        "mcp_tool_grants",
        {
            WRITE_TOOL: {
                "read_only": False,
                "approval": "per_call",
                "tenants": ["t1"],
                "roles": ["manager"],
            },
        },
    )
    monkeypatch.setattr(mcp_approvals, "LEDGER", mcp_approvals.TicketLedger())
    mcp_approvals.clear_audit()
    return settings


@pytest.fixture
def clean_approvals():
    from forgeflow.repositories.memory.policy_repo import clear_policy_store

    clear_policy_store()
    yield
    clear_policy_store()


# --------------------------------------------------------------------------- #
# The preview a human reads                                                   #
# --------------------------------------------------------------------------- #
def test_preview_redacts_secrets_and_keeps_the_rest_readable():
    preview = mcp_approvals.payload_preview(
        {
            "invoice_id": "INV-7",
            "amount": 1200,
            "authorization": "Bearer sk-live-should-not-leak",
            "smtp_password": "hunter2",
            "note": "pay on receipt",
        }
    )
    assert "sk-live-should-not-leak" not in preview
    assert "hunter2" not in preview
    assert preview.count(mcp_approvals.PREVIEW_REDACTED) == 2
    body = json.loads(preview)
    assert body["invoice_id"] == "INV-7"
    assert body["amount"] == 1200
    assert body["note"] == "pay on receipt"


def test_preview_states_the_real_length_instead_of_silently_cutting():
    long_text = "报销" * 500  # 1000 chars
    body = json.loads(mcp_approvals.payload_preview({"body": long_text}))
    assert body["body"].endswith("…(len=1000)")
    assert len(body["body"]) < 200


def test_preview_is_bounded_for_a_huge_payload_and_stays_linear():
    """20 KB of Chinese must cost milliseconds, not a ReDoS stall."""
    payload = {
        "intent": "生成季度财报" * 2000,
        "paths": [f"/d/工作/文件{i}.xlsx" for i in range(500)],
    }
    started = time.monotonic()
    preview = mcp_approvals.payload_preview(payload)
    elapsed = time.monotonic() - started
    assert len(preview) <= mcp_approvals.PREVIEW_MAX_CHARS + 40
    assert elapsed < 1.0
    # Deep nesting cannot blow the recursion either.
    nested: dict = {"a": {}}
    cursor = nested["a"]
    for _ in range(50):
        cursor["a"] = {}
        cursor = cursor["a"]
    assert isinstance(mcp_approvals.payload_preview(nested), str)


def test_preview_never_raises_on_an_exotic_payload():
    class Weird:
        def __str__(self) -> str:
            return "weird"

    assert "weird" in mcp_approvals.payload_preview({"value": Weird()})


# --------------------------------------------------------------------------- #
# The pending request binds the payload                                       #
# --------------------------------------------------------------------------- #
async def test_pending_request_records_digest_and_a_redacted_preview(secured, clean_approvals):
    from forgeflow.repositories import get_policy_repository

    arguments = {"invoice_id": "INV-9", "token": "sk-do-not-store"}
    approval_id = await request_approval(
        tenant="t1", actor="u1", run_id="run-1", tool=WRITE_TOOL, arguments=arguments
    )
    assert approval_id
    rows = await get_policy_repository().list_approvals("t1", status="pending")
    assert len(rows) == 1
    row = rows[0]
    assert row.payload_digest == args_digest(arguments)
    assert "INV-9" in row.note
    assert "sk-do-not-store" not in row.note
    assert mcp_approvals.PREVIEW_REDACTED in row.note


async def test_retry_is_idempotent_but_a_different_payload_is_not(secured, clean_approvals):
    """Same payload retried → one row; new payload → its own decision."""
    from forgeflow.repositories import get_policy_repository

    repo = get_policy_repository()
    first = await request_approval(
        tenant="t1", actor="u1", run_id="run-1", tool=WRITE_TOOL, arguments={"amount": 10}
    )
    again = await request_approval(
        tenant="t1", actor="u1", run_id="run-1", tool=WRITE_TOOL, arguments={"amount": 10}
    )
    assert first == again
    assert len(await repo.list_approvals("t1", status="pending")) == 1

    other = await request_approval(
        tenant="t1", actor="u1", run_id="run-1", tool=WRITE_TOOL, arguments={"amount": 999}
    )
    assert other and other != first
    rows = await repo.list_approvals("t1", status="pending")
    assert len(rows) == 2
    assert {row.payload_digest for row in rows} == {
        args_digest({"amount": 10}),
        args_digest({"amount": 999}),
    }


# --------------------------------------------------------------------------- #
# The gate: an approval authorises exactly one payload                        #
# --------------------------------------------------------------------------- #
def _approved(digest: str, *, tenant: str = "t1") -> ApprovalRecord:
    from datetime import UTC, datetime

    return ApprovalRecord(
        tenant_id=tenant,
        run_id="run-1",
        risk_level="high",
        requested_action=f"{APPROVAL_KEY_PREFIX}{WRITE_TOOL}",
        # FF-R1 P1 adaptation: approvals are bound to their requester
        # on main; the row names the test actor.
        requester="u1",
        approver="operator",
        decision="approve",
        status="approved",
        resolved_at=datetime.now(UTC),
        payload_digest=digest,
    )


async def test_approved_payload_runs_and_a_different_one_does_not(secured, clean_approvals):
    from forgeflow.repositories import get_policy_repository

    approved_args = {"amount": 10, "to": "a@b"}
    await get_policy_repository().save_approval(_approved(args_digest(approved_args)))

    ticket = await authorize_write(
        tenant="t1",
        actor="u1",
        role="manager",
        run_id="run-1",
        tool=WRITE_TOOL,
        arguments=approved_args,
    )
    assert ticket.digest == args_digest(approved_args)

    mcp_approvals.clear_audit()
    with pytest.raises(WriteApprovalRequired):
        await authorize_write(
            tenant="t1",
            actor="u1",
            role="manager",
            run_id="run-1",
            tool=WRITE_TOOL,
            arguments={"amount": 10, "to": "c@d"},
        )
    outcomes = [entry["outcome"] for entry in mcp_approvals.recent_audit()]
    assert NO_APPROVAL in outcomes
    # …and the refusal left a *pending* request for the payload it could not clear.
    pending = await get_policy_repository().list_approvals("t1", status="pending")
    assert [row.payload_digest for row in pending] == [args_digest({"amount": 10, "to": "c@d"})]


async def test_an_approval_without_a_payload_authorises_nothing(secured, clean_approvals):
    """Pre-043 rows must fail closed, and say so, instead of becoming wildcards."""
    from forgeflow.repositories import get_policy_repository

    await get_policy_repository().save_approval(_approved(""))

    with pytest.raises(WriteApprovalRequired):
        await authorize_write(
            tenant="t1",
            actor="u1",
            role="manager",
            run_id="run-1",
            tool=WRITE_TOOL,
            arguments={"amount": 10},
        )
    outcomes = [entry["outcome"] for entry in mcp_approvals.recent_audit()]
    assert "legacy_approval" in outcomes
    assert NO_APPROVAL in outcomes


# --------------------------------------------------------------------------- #
# Who decides                                                                 #
# --------------------------------------------------------------------------- #
def test_the_requester_cannot_decide_their_own_mcp_write(secured, clean_approvals, monkeypatch):
    """Behavioural, not structural: the pre-fix route answers 200 here.

    The run header is stubbed so the *decision policy* is what is measured — a
    baseline tree has no ``self_approval_problem`` to import, and this test must
    fail by returning 200, not by an ImportError.
    """
    _stub_run_header(monkeypatch, actor="u1")
    approval_id = _pending()

    with _client_for("u1") as client:
        seen = client.get("/approvals").json()["items"]
        assert approval_id in {item["id"] for item in seen}
        denied = client.post(f"/approvals/{approval_id}/decision", json={"decision": "approve"})
        assert denied.status_code == 403, (
            f"requester's own approval was decided: {denied.status_code}"
        )
        assert "someone other than the requester" in denied.json()["detail"]

    # A second manager in the same tenant can decide it.
    with _client_for("u2") as client:
        assert approval_id in {item["id"] for item in client.get("/approvals").json()["items"]}
        decided = client.post(f"/approvals/{approval_id}/decision", json={"decision": "approve"})
        assert decided.status_code == 200, decided.text


def _stub_run_header(monkeypatch, *, actor: str) -> None:
    """``load_run_header`` without a database: the owner of every run is ``actor``."""
    import forgeflow.runtime.dispatcher as dispatcher

    async def fake_header(tenant: str, run_id: str):
        return SimpleNamespace(run_id=run_id, tenant_id=tenant, actor_user_id=actor)

    monkeypatch.setattr(dispatcher, "load_run_header", fake_header)


def _client_for(user_id: str, role: str = "manager") -> TestClient:
    from forgeflow.api.dependencies import get_current_user
    from forgeflow.api.hub_deps import resolve_tenant
    from forgeflow.api.routers import approvals_hub

    app = FastAPI()
    app.include_router(approvals_hub.router, prefix="/approvals")
    app.dependency_overrides[resolve_tenant] = lambda: "t1"
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(user_id=user_id, role=role)
    return TestClient(app)


ARGUMENTS = {"amount": 10, "to": "a@b"}


def _pending(*, requester: str = "u1", digest: str | None = None) -> str:
    """A pending MCP write approval, written the way the gate writes it.

    Built directly (not through ``request_approval``) so this file can measure
    *decision policy* on a tree that predates the digest-aware requester.
    """
    approval = ApprovalRecord(
        tenant_id="t1",
        run_id="run-1",
        risk_level="high",
        requested_action=f"{APPROVAL_KEY_PREFIX}{WRITE_TOOL}",
        requester=requester,
        status="pending",
        payload_digest=args_digest(ARGUMENTS) if digest is None else digest,
    )
    asyncio.run(get_policy_repository().save_approval(approval))
    return approval.id


def test_reviewer_sees_and_can_decide_while_the_requester_is_refused(
    secured, clean_approvals, monkeypatch
):
    """The end-to-end path: request → another human approves → write is authorised."""
    _stub_run_header(monkeypatch, actor="u1")
    approval_id = _pending()

    # The requester's own seat: visible (it is their request) but not decidable.
    with _client_for("u1") as client:
        assert approval_id in {item["id"] for item in client.get("/approvals").json()["items"]}
        assert (
            client.post(
                f"/approvals/{approval_id}/decision", json={"decision": "approve"}
            ).status_code
            == 403
        )

    with _client_for("u2") as client:
        assert approval_id in {item["id"] for item in client.get("/approvals").json()["items"]}
        decided = client.post(f"/approvals/{approval_id}/decision", json={"decision": "approve"})
        assert decided.status_code == 200, decided.text
        assert decided.json()["payload_digest"] == args_digest(ARGUMENTS)

    ticket = asyncio.run(
        authorize_write(
            tenant="t1",
            actor="u1",
            role="manager",
            run_id="run-1",
            tool=WRITE_TOOL,
            arguments=ARGUMENTS,
        )
    )
    assert ticket.approval_id == approval_id
    assert ticket.digest == args_digest(ARGUMENTS)


def test_a_viewer_cannot_lurk_in_the_reviewer_pool(secured, clean_approvals, monkeypatch):
    """Visibility is reviewer-seat or owner — a plain viewer sees nothing."""
    _stub_run_header(monkeypatch, actor="u1")
    approval_id = _pending()
    with _client_for("u3", role="viewer") as client:
        assert approval_id not in {item["id"] for item in client.get("/approvals").json()["items"]}


def test_self_approval_opt_in_is_honoured_by_the_api(secured, clean_approvals, monkeypatch):
    _stub_run_header(monkeypatch, actor="u1")
    monkeypatch.setattr(secured, "mcp_write_allow_self_approval", True)
    approval_id = _pending()
    with _client_for("u1") as client:
        decided = client.post(f"/approvals/{approval_id}/decision", json={"decision": "approve"})
        assert decided.status_code == 200, decided.text
