"""Outbound email over SMTP — a real transport, or an honest refusal.

The tool that used to own this job (``mcp/server/tools/email_tools.py``) appended
to an in-process list and returned ``{"success": True, "status": "sent"}``. No
socket was ever opened, so "邮件已发送" was unfalsifiable: the run looked
delivered while nothing left the machine. This module closes that hole by making
the *only* way to claim ``sent`` a completed SMTP transaction.

Three outcomes, and no fourth:

* ``sent``            — the relay accepted the message (250 on ``DATA``).
* ``not_configured``  — no relay/from configured. Nothing was attempted.
* ``failed``          — a socket, TLS, auth or protocol error; ``error`` says which.

Everything runs in a worker thread (``smtplib`` is blocking) with a socket
timeout, so a hung relay cannot stall the event loop. The password is never
logged and never appears in a returned payload.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any

from forgeflow.connectors.base import ConnectorError

logger = logging.getLogger(__name__)

__all__ = ["EmailDelivery", "SmtpEmailSender", "get_email_sender", "reset_email_sender"]


@dataclass(frozen=True)
class EmailDelivery:
    """The outcome of one delivery attempt — the only thing a caller may report.

    ``status`` is the verdict; ``sent`` is derived from it so there is exactly
    one source of truth. ``message_id`` is the RFC-5322 ``Message-ID`` this
    process generated (client-side, therefore always present on a send attempt);
    it is deliberately *not* described as a provider/queue id, which SMTP does
    not give us.
    """

    status: str
    message_id: str = ""
    recipients: tuple[str, ...] = ()
    response: str = ""
    error: str = ""
    refused: dict[str, Any] = field(default_factory=dict)

    @property
    def sent(self) -> bool:
        return self.status == "sent"

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "success": self.sent,
            "status": self.status,
            "message_id": self.message_id,
            "recipients": list(self.recipients),
        }
        if self.response:
            payload["smtp_response"] = self.response
        if self.error:
            payload["error"] = self.error
        if self.refused:
            payload["refused"] = self.refused
        return payload


class SmtpEmailSender:
    """SMTP transport bound to the current settings (read lazily per attempt)."""

    def __init__(self, settings: Any | None = None) -> None:
        self._settings = settings

    def _settings_obj(self) -> Any:
        if self._settings is not None:
            return self._settings
        from forgeflow.config import get_settings

        return get_settings()

    def configuration_problem(self) -> str:
        """Why a real send is impossible right now (``""`` when it is possible)."""
        settings = self._settings_obj()
        host = str(getattr(settings, "smtp_host", "") or "").strip()
        sender = str(getattr(settings, "smtp_from", "") or "").strip()
        if not host:
            return "SMTP relay is not configured (smtp_host is empty)"
        if not sender:
            return "SMTP sender is not configured (smtp_from is empty)"
        if "@" not in sender:
            return f"smtp_from is not an email address: {sender!r}"
        return ""

    async def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        cc: Iterable[str] | None = None,
    ) -> EmailDelivery:
        """Attempt delivery. Never raises for a normal transport failure."""
        recipients = tuple(r for r in [to, *(cc or [])] if r)
        problem = self.configuration_problem()
        if problem:
            return EmailDelivery(status="not_configured", recipients=recipients, error=problem)
        message = self._build(to=to, subject=subject, body=body, cc=list(cc or []))
        try:
            return await asyncio.to_thread(self._deliver, message, recipients)
        except (ConnectorError, OSError, ssl.SSLError, smtplib.SMTPException) as exc:
            detail = f"{type(exc).__name__}: {exc}"
            logger.warning("SMTP delivery failed for %s: %s", ", ".join(recipients), detail)
            return EmailDelivery(
                status="failed",
                message_id=str(message["Message-ID"] or ""),
                recipients=recipients,
                error=detail[:500],
            )

    # -- internals --------------------------------------------------------- #

    def _build(self, *, to: str, subject: str, body: str, cc: list[str]) -> EmailMessage:
        settings = self._settings_obj()
        message = EmailMessage()
        message["From"] = str(getattr(settings, "smtp_from", "") or "").strip()
        message["To"] = to
        if cc:
            message["Cc"] = ", ".join(cc)
        message["Subject"] = subject
        message["Date"] = formatdate(localtime=True)
        domain = message["From"].split("@")[-1] or "forgeflow.local"
        message["Message-ID"] = make_msgid(idstring=uuid.uuid4().hex[:12], domain=domain)
        message.set_content(body)
        return message

    def _deliver(self, message: EmailMessage, recipients: tuple[str, ...]) -> EmailDelivery:
        settings = self._settings_obj()
        host = str(getattr(settings, "smtp_host", "")).strip()
        port = int(getattr(settings, "smtp_port", 587))
        timeout = float(getattr(settings, "smtp_timeout_seconds", 10.0))
        username = str(getattr(settings, "smtp_username", "") or "")
        password = getattr(settings, "smtp_password", None)
        secret = (
            password.get_secret_value()
            if hasattr(password, "get_secret_value")
            else str(password or "")
        )
        context = ssl.create_default_context()

        if bool(getattr(settings, "smtp_ssl", False)):
            client: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
        else:
            client = smtplib.SMTP(host, port, timeout=timeout)
        try:
            client.ehlo()
            if bool(getattr(settings, "smtp_starttls", True)) and not isinstance(
                client, smtplib.SMTP_SSL
            ):
                client.starttls(context=context)
                client.ehlo()
            if username:
                client.login(username, secret)
            refused = client.send_message(message, to_addrs=list(recipients))
        finally:
            try:
                client.quit()
            except Exception:  # noqa: BLE001 — a broken QUIT must not mask the send
                client.close()

        if refused:
            # A partial delivery is not a success: say so and name who was refused.
            return EmailDelivery(
                status="failed",
                message_id=str(message["Message-ID"] or ""),
                recipients=recipients,
                error=f"relay refused {len(refused)} recipient(s)",
                refused={str(k): (v[0], str(v[1])) for k, v in refused.items()},
            )
        return EmailDelivery(
            status="sent",
            message_id=str(message["Message-ID"] or ""),
            recipients=recipients,
            response=f"{host}:{port} accepted the message",
        )


_SENDER: SmtpEmailSender | None = None


def get_email_sender() -> SmtpEmailSender:
    global _SENDER
    if _SENDER is None:
        _SENDER = SmtpEmailSender()
    return _SENDER


def reset_email_sender() -> None:
    """Drop the singleton so a test can bind it to a different ``Settings``."""
    global _SENDER
    _SENDER = None
