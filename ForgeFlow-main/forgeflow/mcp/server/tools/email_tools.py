"""MCP tools: email drafting and sending.

Sending goes through :mod:`forgeflow.connectors.email`, i.e. a real SMTP
transaction when one is configured. When it is not configured the tool says so
(``status="not_configured"``, ``success=False``) instead of reporting a
delivery that never happened — "邮件已发送" is only ever claimed after the relay
accepted the message.
"""

from __future__ import annotations

import logging
import uuid
from collections import deque
from datetime import UTC, datetime

from fastmcp import FastMCP

logger = logging.getLogger(__name__)

router = FastMCP("email-tools")

#: Bounded audit trail of delivery *attempts* (never a substitute for a send).
#: Capped because this is a debug surface, not a mail store.
_sent_emails: deque[dict] = deque(maxlen=200)


@router.tool()
async def draft_email(
    to: str,
    subject: str,
    context: str,
    tone: str = "professional",
) -> dict:
    """Generate an email draft using the provided context.

    Args:
        to: Recipient email address
        subject: Email subject line
        context: Key points and information to include
        tone: Writing tone — professional | friendly | formal

    Returns:
        Email draft with subject and body
    """
    # In production this would call an LLM or email template engine
    body = f"""Dear {to.split('@')[0].replace('.', ' ').title()},

I hope this message finds you well.

{context}

I'd love to schedule a brief 30-minute conversation to learn more about your
goals and share how ForgeFlow's enterprise AI platform could accelerate your outcomes.

Are you available for a call this week or next?

Best regards,
ForgeFlow Sales Team
sales@forgeflow.ai | calendly.com/forgeflow"""

    draft = {
        "to": to,
        "subject": subject,
        "body": body,
        "tone": tone,
        "draft_id": str(uuid.uuid4()),
        "created_at": datetime.now(UTC).isoformat(),
    }
    logger.info("Email drafted for %s | subject: %s", to, subject)
    return draft


@router.tool()
async def send_email(
    to: str,
    subject: str,
    body: str,
    allowed_domains: list[str] | None = None,
    cc: list[str] | None = None,
) -> dict:
    """Send an email — requires an explicit recipient-domain allowlist.

    Exfiltration control (SECURITY_AUDIT.md C-5): the LLM picks the recipients,
    so the *server* decides which domains are reachable. ``Settings.
    email_allowed_domains`` wins whenever it is set; otherwise the caller must
    supply ``allowed_domains``. An empty allowlist is a refusal, never "allow
    everything".

    Delivery is real or explicitly absent — see
    :mod:`forgeflow.connectors.email`. The returned ``status`` is one of
    ``sent`` / ``not_configured`` / ``failed`` / ``rejected``, and only ``sent``
    comes with ``success: True``.

    Args:
        to: Primary recipient email
        subject: Email subject line
        body: Full email body text
        allowed_domains: fnmatch patterns the recipient must satisfy — used only
            when the server-side allowlist is empty
        cc: Optional CC recipients
    """
    from forgeflow.config import get_settings
    from forgeflow.connectors.email import get_email_sender
    from forgeflow.security.email_allowlist import EmailNotAllowed, require_allowed

    settings = get_settings()
    server_allowlist = list(getattr(settings, "email_allowed_domains", []) or [])
    effective = server_allowlist or list(allowed_domains or [])
    allowlist_source = "server" if server_allowlist else "caller"
    if not effective:
        logger.warning("Refusing send_email: no recipient allowlist configured")
        return {
            "success": False,
            "status": "rejected",
            "error": (
                "no recipient allowlist: set email_allowed_domains on the server "
                "or pass allowed_domains"
            ),
        }

    recipients = [to] + list(cc or [])
    try:
        for r in recipients:
            require_allowed(r, effective)
    except EmailNotAllowed as exc:
        logger.warning("Refusing send_email: %s", exc)
        return {
            "success": False,
            "status": "rejected",
            "error": str(exc),
            "allowlist_source": allowlist_source,
        }

    delivery = await get_email_sender().send(to=to, subject=subject, body=body, cc=cc)
    record = {
        "message_id": delivery.message_id,
        "to": to,
        "cc": cc or [],
        "subject": subject,
        "status": delivery.status,
        "sent_at": datetime.now(UTC).isoformat() if delivery.sent else None,
        "error": delivery.error,
        "allowlist_source": allowlist_source,
    }
    _sent_emails.append(record)
    if delivery.sent:
        logger.info("Email sent | to=%s | subject=%s | id=%s", to, subject, delivery.message_id)
    else:
        logger.warning(
            "Email NOT sent (%s) | to=%s | subject=%s | reason=%s",
            delivery.status,
            to,
            subject,
            delivery.error,
        )
    return delivery.as_dict() | {"allowlist_source": allowlist_source}
