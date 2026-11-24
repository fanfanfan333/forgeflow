"""FF-R1 P4-FIX — the two S1 findings on the executor email path.

1. **Idempotency + approval**: re-running an approved executor
   workflow used to send the proposal email twice —
   ``_send_proposal_email`` never checked the workflow's one-time
   human approval and kept no record of having sent. Now: no
   ``approval_status="approved"`` in the workflow state → no send;
   a send is recorded under an idempotency key
   (workflow + recipient + content digest) and a re-run / resume
   returns the previous outcome without touching the relay again.
2. **Secret hygiene**: an SMTP exception's raw text (which can
   echo credentials) used to flow into ``EmailDelivery.error``,
   the returned dict and the logs. Error text now keeps the
   exception type plus a redacted message — the configured
   password and credential-shaped fragments are stripped by one
   shared redaction helper.

All transport is a stub; the "secret" below is an obvious fake.
"""

from __future__ import annotations

import logging
import smtplib
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from forgeflow.connectors import email as email_connector

FAKE_SECRET = "FAKE-SECRET-12345"


class _StubSMTP:
    instances: list[_StubSMTP] = []
    fail_login_with: Exception | None = None

    def __init__(self, host, port, timeout=None, context=None):
        self.calls: list[str] = []
        self.message = None
        _StubSMTP.instances.append(self)

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append("login")
        if _StubSMTP.fail_login_with is not None:
            raise _StubSMTP.fail_login_with

    def send_message(self, message, to_addrs=None):
        self.calls.append("send_message")
        self.message = message
        return {}

    def quit(self):
        self.calls.append("quit")

    def close(self):
        pass


def _settings(**overrides):
    base = dict(
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_username="sales@forgeflow.example",
        smtp_password=SecretStr(FAKE_SECRET),
        smtp_from="sales@forgeflow.example",
        smtp_starttls=True,
        smtp_ssl=False,
        smtp_timeout_seconds=5.0,
        email_allowed_domains=[],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture()
def sender(monkeypatch):
    _StubSMTP.instances.clear()
    _StubSMTP.fail_login_with = None
    monkeypatch.setattr(email_connector.smtplib, "SMTP", _StubSMTP)
    installed = email_connector.SmtpEmailSender(settings=_settings())
    monkeypatch.setattr(email_connector, "_SENDER", installed)
    return installed


def _send_count() -> int:
    return sum(inst.calls.count("send_message") for inst in _StubSMTP.instances)


async def _proposal_email(agent, **kwargs):
    defaults = dict(
        lead_data={"contact_email": "ceo@stripe.com"},
        proposal={"executive_summary": "Great fit."},
        company="Stripe",
        allowed_domains=["stripe.com"],
        workflow_id="wf-1",
    )
    defaults.update(kwargs)
    return await agent._send_proposal_email(**defaults)


@pytest.mark.asyncio
async def test_rerun_of_an_approved_workflow_sends_exactly_once(sender):
    from forgeflow.agents.executor import ExecutorAgent

    agent = ExecutorAgent.__new__(ExecutorAgent)
    # P4-FIX2 adaptation: the ledger moved from the sender into the
    # workflow state, so the two runs share the state dict — the
    # assertions are unchanged.
    deliveries: dict = {}
    first = await _proposal_email(agent, approval_status="approved", email_deliveries=deliveries)
    second = await _proposal_email(agent, approval_status="approved", email_deliveries=deliveries)
    assert first == ["email_sent"]
    assert second == ["email_sent"]  # the previous outcome, returned as-is
    assert _send_count() == 1


@pytest.mark.asyncio
async def test_no_approval_no_send(sender):
    from forgeflow.agents.executor import ExecutorAgent, incomplete_actions

    agent = ExecutorAgent.__new__(ExecutorAgent)
    for status in ("", "pending", "rejected"):
        actions = await _proposal_email(agent, approval_status=status)
        assert actions == ["email_not_approved"]
        assert incomplete_actions(actions) == ["email_not_approved"]
    assert _send_count() == 0


@pytest.mark.asyncio
async def test_smtp_error_never_leaks_the_configured_secret(sender, caplog):
    _StubSMTP.fail_login_with = smtplib.SMTPAuthenticationError(
        535, f"auth failed for password={FAKE_SECRET}".encode()
    )
    with caplog.at_level(logging.WARNING):
        delivery = await sender.send(to="ceo@stripe.com", subject="Proposal", body="Hi")
    assert delivery.status == "failed"
    # The diagnostic keeps the exception TYPE…
    assert "SMTPAuthenticationError" in delivery.error
    # …but the secret appears nowhere: not in the error, not in the
    # returned dict, not in the logs.
    assert FAKE_SECRET not in delivery.error
    assert FAKE_SECRET not in str(delivery.as_dict())
    assert FAKE_SECRET not in caplog.text
