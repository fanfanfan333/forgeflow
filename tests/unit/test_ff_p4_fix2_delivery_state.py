"""FF-R1 P4-FIX2 — the idempotency record lives in the workflow state.

P4-FIX kept the sent-record on the sender object (process memory);
a resume in a NEW process sent a second copy. The record now lives
in ``state["email_deliveries"]`` — the dict the graph checkpoints —
keyed by the same idempotency key, as a two-phase entry:

* before the send: ``{"status": "sending"}`` is written;
* after the relay accepts: ``{"status": "sent", "message_id": …}``;
* resume meeting ``sent`` returns the recorded outcome, no send;
* resume meeting ``sending`` (crashed mid-send — the mail may or
  may not have gone out) does NOT resend: the outcome is
  ``email_delivery_uncertain``, for a human to resolve;
* ``failed`` / unapproved attempts stay retryable.

Approval gating and SMTP-error redaction are P4-FIX's and are not
re-tested here beyond the approval the helper passes.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from forgeflow.connectors import email as email_connector


class _StubSMTP:
    instances: list[_StubSMTP] = []

    def __init__(self, host, port, timeout=None, context=None):
        self.calls: list[str] = []
        _StubSMTP.instances.append(self)

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append("login")

    def send_message(self, message, to_addrs=None):
        self.calls.append("send_message")
        return {}

    def quit(self):
        self.calls.append("quit")

    def close(self):
        pass


def _settings():
    return SimpleNamespace(
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_username="sales@forgeflow.example",
        smtp_password=SecretStr("FAKE-SECRET-12345"),
        smtp_from="sales@forgeflow.example",
        smtp_starttls=True,
        smtp_ssl=False,
        smtp_timeout_seconds=5.0,
        email_allowed_domains=[],
    )


@pytest.fixture()
def install_sender(monkeypatch):
    _StubSMTP.instances.clear()
    monkeypatch.setattr(email_connector.smtplib, "SMTP", _StubSMTP)

    def install():
        sender = email_connector.SmtpEmailSender(settings=_settings())
        monkeypatch.setattr(email_connector, "_SENDER", sender)
        return sender

    install()
    return install


def _send_count() -> int:
    return sum(inst.calls.count("send_message") for inst in _StubSMTP.instances)


async def _call(agent, deliveries):
    return await agent._send_proposal_email(
        {"contact_email": "ceo@stripe.com"},
        {"executive_summary": "Great fit."},
        "Stripe",
        allowed_domains=["stripe.com"],
        approval_status="approved",
        workflow_id="wf-1",
        email_deliveries=deliveries,
    )


@pytest.mark.asyncio
async def test_resume_with_fresh_instances_does_not_resend(install_sender):
    from forgeflow.agents.executor import ExecutorAgent, proposal_email_key

    deliveries: dict = {}
    first = await _call(ExecutorAgent.__new__(ExecutorAgent), deliveries)
    assert first == ["email_sent"]
    assert _send_count() == 1
    key = proposal_email_key("wf-1", "ceo@stripe.com", "Proposal for Stripe", "Great fit.")
    assert deliveries[key]["status"] == "sent"
    assert deliveries[key]["message_id"]

    # Resume "in a new process": a new executor AND a new sender
    # instance — only the checkpointed state carries over.
    install_sender()
    second = await _call(ExecutorAgent.__new__(ExecutorAgent), deliveries)
    assert second == ["email_sent"]  # the recorded outcome
    assert _send_count() == 1


@pytest.mark.asyncio
async def test_resume_during_sending_is_uncertain_and_never_resends(install_sender):
    from forgeflow.agents.executor import ExecutorAgent, incomplete_actions, proposal_email_key

    key = proposal_email_key("wf-1", "ceo@stripe.com", "Proposal for Stripe", "Great fit.")
    deliveries = {key: {"status": "sending"}}
    actions = await _call(ExecutorAgent.__new__(ExecutorAgent), deliveries)
    assert actions == ["email_delivery_uncertain"]
    assert incomplete_actions(actions) == ["email_delivery_uncertain"]
    assert _send_count() == 0


@pytest.mark.asyncio
async def test_a_failed_record_stays_retryable(install_sender):
    from forgeflow.agents.executor import ExecutorAgent, proposal_email_key

    key = proposal_email_key("wf-1", "ceo@stripe.com", "Proposal for Stripe", "Great fit.")
    deliveries = {key: {"status": "failed"}}
    actions = await _call(ExecutorAgent.__new__(ExecutorAgent), deliveries)
    assert actions == ["email_sent"]
    assert _send_count() == 1
    assert deliveries[key]["status"] == "sent"
