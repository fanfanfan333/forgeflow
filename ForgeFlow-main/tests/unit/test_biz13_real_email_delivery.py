"""BIZ-13 — outbound email must be a real SMTP transaction or an honest refusal.

The defect: ``send_email`` appended to an in-process list and returned
``{"success": True, "status": "sent"}`` without opening a socket. The run looked
delivered while nothing left the machine, and no test could tell the difference.

Every case below fails on the old implementation:

* ``not_configured``  — the old code said ``sent``.
* ``sent``            — the old code never built a message, so the relay stub
  receives nothing and the From/Subject/Message-ID assertions are impossible.
* ``failed``          — an SMTP error was previously unreachable code.
* allowlist precedence — the caller's list was the only list.
"""

from __future__ import annotations

import smtplib
from types import SimpleNamespace

import pytest

from forgeflow.connectors import email as email_connector
from forgeflow.mcp.server.tools import email_tools


class _StubSMTP:
    """Records what a real ``smtplib.SMTP`` would have been asked to do."""

    instances: list[_StubSMTP] = []

    def __init__(self, host: str, port: int, timeout: float | None = None, context=None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls: list[str] = []
        self.message = None
        self.credentials: tuple[str, str] | None = None
        self.recipients: list[str] = []
        self.refused: dict = {}
        self.fail_with: Exception | None = None
        _StubSMTP.instances.append(self)

    def ehlo(self) -> None:
        self.calls.append("ehlo")

    def starttls(self, context=None) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append("login")
        self.credentials = (user, password)

    def send_message(self, message, to_addrs=None):
        self.calls.append("send_message")
        if self.fail_with is not None:
            raise self.fail_with
        self.message = message
        self.recipients = list(to_addrs or [])
        return self.refused

    def quit(self) -> None:
        self.calls.append("quit")

    def close(self) -> None:
        self.calls.append("close")


def _settings(**overrides):
    base = dict(
        smtp_host="",
        smtp_port=587,
        smtp_username="",
        smtp_password=SimpleNamespace(get_secret_value=lambda: "s3cret-app-password"),
        smtp_from="",
        smtp_starttls=True,
        smtp_ssl=False,
        smtp_timeout_seconds=5.0,
        email_allowed_domains=[],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture()
def smtp(monkeypatch):
    """Install the stub transport and a configurable Settings object."""

    _StubSMTP.instances.clear()
    monkeypatch.setattr(email_connector.smtplib, "SMTP", _StubSMTP)

    def install(**overrides):
        settings = _settings(**overrides)
        monkeypatch.setattr("forgeflow.config.get_settings", lambda: settings)
        monkeypatch.setattr(
            email_connector, "_SENDER", email_connector.SmtpEmailSender(settings=settings)
        )
        email_tools._sent_emails.clear()
        return settings

    return install


async def test_unconfigured_relay_reports_not_configured_never_sent(smtp):
    """No relay ⇒ ``not_configured``; the old code returned ``sent`` here."""
    smtp()  # smtp_host / smtp_from empty

    result = await email_tools.send_email(
        to="ceo@stripe.com",
        subject="Proposal",
        body="hello",
        allowed_domains=["stripe.com"],
    )

    assert result["success"] is False
    assert result["status"] == "not_configured"
    assert "smtp_host" in result["error"]
    assert _StubSMTP.instances == [], "an unconfigured relay must not be dialled"
    # The audit trail records the truth, not an aspiration.
    assert [r["status"] for r in email_tools._sent_emails] == ["not_configured"]


async def test_configured_relay_gets_a_real_message_and_is_reported_as_sent(smtp):
    """A configured relay receives a real RFC-5322 message; then we may say sent."""
    smtp(
        smtp_host="smtp.example.com",
        smtp_from="sales@forgeflow.example",
        smtp_username="sales@forgeflow.example",
    )

    result = await email_tools.send_email(
        to="ceo@stripe.com",
        subject="Proposal: ForgeFlow × Stripe",
        body="Dear CEO, here is the proposal.",
        allowed_domains=["stripe.com"],
        cc=["cto@stripe.com"],
    )

    assert result["success"] is True, result
    assert result["status"] == "sent"
    assert result["message_id"].endswith(">")
    assert result["recipients"] == ["ceo@stripe.com", "cto@stripe.com"]

    client = _StubSMTP.instances[0]
    assert client.host == "smtp.example.com" and client.port == 587
    assert client.timeout == 5.0
    assert client.calls[:3] == ["ehlo", "starttls", "ehlo"]
    assert client.credentials == ("sales@forgeflow.example", "s3cret-app-password")
    assert client.recipients == ["ceo@stripe.com", "cto@stripe.com"]

    message = client.message
    assert message["From"] == "sales@forgeflow.example"
    assert message["To"] == "ceo@stripe.com"
    assert message["Cc"] == "cto@stripe.com"
    assert message["Subject"] == "Proposal: ForgeFlow × Stripe"
    assert message["Message-ID"] == result["message_id"]
    assert "Dear CEO, here is the proposal." in message.get_content()

    # The password never travels back to the caller.
    assert "s3cret-app-password" not in str(result)


async def test_relay_failure_is_a_failure_not_a_send(smtp):
    """A 550 from the relay must surface as ``failed`` + ``success False``."""
    smtp(smtp_host="smtp.example.com", smtp_from="sales@forgeflow.example")

    class _Failing(_StubSMTP):
        def __init__(self, *a, **kw):  # noqa: ANN002, ANN003
            super().__init__(*a, **kw)
            self.fail_with = smtplib.SMTPDataError(550, b"relay says no")

    import forgeflow.connectors.email as mod

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mod.smtplib, "SMTP", _Failing)
        result = await email_tools.send_email(
            to="ceo@stripe.com", subject="x", body="y", allowed_domains=["stripe.com"]
        )

    assert result["success"] is False
    assert result["status"] == "failed"
    assert "SMTPDataError" in result["error"]
    assert [r["status"] for r in email_tools._sent_emails] == ["failed"]


async def test_refused_recipient_is_not_reported_as_sent(smtp):
    """A partial delivery is a failure that names the refused recipient."""
    smtp(smtp_host="smtp.example.com", smtp_from="sales@forgeflow.example")
    original_init = _StubSMTP.__init__

    class _Refusing(_StubSMTP):
        def __init__(self, *a, **kw):  # noqa: ANN002, ANN003
            original_init(self, *a, **kw)
            self.refused = {"ceo@stripe.com": (550, b"mailbox unavailable")}

    import forgeflow.connectors.email as mod

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mod.smtplib, "SMTP", _Refusing)
        result = await email_tools.send_email(
            to="ceo@stripe.com", subject="x", body="y", allowed_domains=["stripe.com"]
        )

    assert result["success"] is False
    assert result["status"] == "failed"
    assert "refused" in result and "ceo@stripe.com" in result["refused"]


async def test_server_allowlist_overrides_the_caller_list(smtp):
    """The model picks recipients; the server picks the reachable domains."""
    smtp(
        smtp_host="smtp.example.com",
        smtp_from="sales@forgeflow.example",
        email_allowed_domains=["acme.example"],
    )

    result = await email_tools.send_email(
        to="ceo@evil.example",
        subject="x",
        body="y",
        allowed_domains=["evil.example"],  # the caller tries to widen it
    )

    assert result["success"] is False
    assert result["status"] == "rejected"
    assert result["allowlist_source"] == "server"
    assert _StubSMTP.instances == [], "a rejected recipient never reaches the relay"


async def test_missing_allowlist_is_a_refusal_not_a_wildcard(smtp):
    smtp(smtp_host="smtp.example.com", smtp_from="sales@forgeflow.example")

    result = await email_tools.send_email(to="ceo@stripe.com", subject="x", body="y")

    assert result["success"] is False
    assert result["status"] == "rejected"
    assert "allowlist" in result["error"]


async def test_executor_reports_the_real_email_outcome(smtp):
    """The sales workflow must not call an unsent email "done"."""
    # Imported inside the test so the baseline counter-proof fails on the
    # behaviour (no ``incomplete_actions`` existed), not on a collection error.
    from forgeflow.agents.executor import ExecutorAgent, incomplete_actions

    smtp()  # unconfigured relay
    agent = ExecutorAgent.__new__(ExecutorAgent)

    actions = await agent._send_proposal_email(
        {"contact_email": "ceo@stripe.com"},
        {"executive_summary": "Great fit."},
        "Stripe",
        allowed_domains=["stripe.com"],
    )
    assert actions == ["email_not_configured"]
    # …and that action really makes the workflow incomplete.
    assert incomplete_actions(actions) == ["email_not_configured"]

    smtp(smtp_host="smtp.example.com", smtp_from="sales@forgeflow.example")
    actions = await agent._send_proposal_email(
        {"contact_email": "ceo@stripe.com"},
        {"executive_summary": "Great fit."},
        "Stripe",
        allowed_domains=["stripe.com"],
    )
    assert actions == ["email_sent"]
    assert incomplete_actions(actions) == []
    assert _StubSMTP.instances[-1].message["Subject"] == "Proposal for Stripe"
