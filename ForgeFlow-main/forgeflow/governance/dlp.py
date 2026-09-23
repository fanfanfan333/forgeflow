"""DLP gate — outbound PII / SSRF / email-allowlist checks (docs §6.2).

Orchestrates the existing atomic guards (`pii_redactor`, `ssrf_guard`,
`email_allowlist`) behind one call so every outbound sink (email, HTTP, memory
write) can be checked identically. Fails closed when ``DLP_ENABLED=true``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from forgeflow.config import get_settings

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids any import cycle
    from forgeflow.governance.dlp_rules import DlpRuleSet

logger = logging.getLogger(__name__)


@dataclass
class DlpResult:
    """Outcome of a DLP scan."""

    blocked: bool = False
    pii_found: bool = False
    categories: list[str] = field(default_factory=list)
    reason: str = ""
    redacted: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _to_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(payload)


class DlpGate:
    """Data-loss-prevention gate for outbound payloads."""

    def __init__(
        self,
        enabled: bool | None = None,
        allowed_email_domains: list[str] | None = None,
        rules: "DlpRuleSet | None" = None,
    ) -> None:
        settings = get_settings()
        self._enabled = settings.dlp_enabled if enabled is None else enabled
        self._allowed_domains = list(allowed_email_domains or [])
        # Configurable categories (INC2 B1). ``None`` ⇒ load the effective rule
        # set: the built-in defaults, optionally overridden by DLP_RULES_FILE.
        # The default set already contains ``cn_id`` (QA P1-1), so a default
        # deployment keeps blocking Chinese resident IDs.
        if rules is None:
            from forgeflow.governance.dlp_rules import load_rule_set

            self._rules = load_rule_set(settings.dlp_rules_file or None)
        else:
            self._rules = rules

    @property
    def rules(self) -> "DlpRuleSet":
        """The active (configurable) DLP rule set."""
        return self._rules

    def scan(self, payload: Any) -> DlpResult:
        """Detect + redact PII. Never blocks (use ``scan_outbound`` for that)."""
        text = _to_text(payload)
        try:
            redacted, matches = self._rules.scan(text)
        except Exception:  # noqa: BLE001 — guard must not crash the caller
            return DlpResult(redacted=text)

        categories = sorted({m.category for m in matches})
        return DlpResult(
            pii_found=bool(matches),
            categories=categories,
            redacted=redacted,
            reason="PII detected" if matches else "",
        )

    def scan_outbound(
        self,
        payload: Any,
        *,
        channel: str = "http",
        recipients: list[str] | None = None,
        url: str | None = None,
    ) -> DlpResult:
        """Full outbound check: PII + channel-specific guards.

        Returns a result with ``blocked=True`` when the payload must not leave.
        """
        result = self.scan(payload)
        if not self._enabled:
            return result

        if result.pii_found:
            result.blocked = True
            result.reason = f"outbound PII blocked: {', '.join(result.categories)}"
            logger.warning("DLP blocked outbound payload | channel=%s pii=%s", channel, result.categories)
            return result

        if channel == "email" and recipients:
            from forgeflow.security.email_allowlist import is_recipient_allowed

            for recipient in recipients:
                if not is_recipient_allowed(recipient, self._allowed_domains):
                    result.blocked = True
                    result.reason = f"recipient '{recipient}' not in email allowlist"
                    logger.warning("DLP blocked email to %s", recipient)
                    return result

        if channel == "http" and url:
            try:
                from forgeflow.security.ssrf_guard import check_url

                check_url(url)
            except Exception as exc:  # noqa: BLE001 — SSRFBlocked or malformed URL
                result.blocked = True
                result.reason = f"outbound URL rejected: {exc}"
                logger.warning("DLP blocked outbound URL | %s", url)
                return result

        return result


async def scan_memory_write(payload: Any, *, enabled: bool | None = None) -> DlpResult:
    """Convenience used before persisting memory/experience content."""
    return DlpGate(enabled=enabled).scan(payload)
