"""Configurable DLP rule set (INC2 B1, docs/sop/05-ARCHITECTURE-INC2.md §2.8).

The DLP gate historically hard-coded a single set of PII categories inside
``forgeflow.security.pii_redactor``. INC2 makes the *rule set* configurable
without losing fail-closed behaviour: deployments can add new categories
(employee IDs, internal project codes, …) or override an existing pattern via a
JSON file pointed at by ``Settings.dlp_rules_file``.

Design:
  * The **built-in default set** (:data:`DEFAULT_RULES`) already contains the
    QA P1-1 fix — the ``cn_id`` Chinese resident-ID category — so a default
    deployment keeps blocking it. ``credit_card`` keeps its Luhn validation and
    is always applied by the underlying redactor.
  * A JSON override may **add** a category or **replace** an existing one. It
    can never *remove* a built-in category, which keeps the gate fail-closed.
  * ``DlpRuleSet.scan`` runs the battle-tested ``pii_redactor.redact`` first and
    then applies the (configurable) regex rules on top, so default behaviour is
    byte-identical to before while custom rules layer in cleanly.

JSON file shape::

    {
      "rules": [
        {"category": "employee_id", "pattern": "EMP-\\\\d{6}", "description": "员工编号"},
        {"category": "phone",       "pattern": "...",         "description": "覆盖内置电话号"}
      ]
    }
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable

logger = logging.getLogger(__name__)

__all__ = [
    "DlpRule",
    "DlpRuleMatch",
    "DEFAULT_RULES",
    "DlpRuleSet",
    "load_rule_set",
]

# Built-in patterns kept in sync with forgeflow.security.pii_redactor. Declared
# here (rather than re-importing private names) so the default *set* is
# introspectable — UI/audit can list exactly which categories are active.
_EMAIL = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
_PHONE = r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)"
_SSN = r"\b\d{3}-\d{2}-\d{4}\b"
_CN_ID = (
    r"(?<!\d)[1-9]\d{5}(?:18|19|20)\d{2}"
    r"(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"
)
_IPV4 = r"\b(?:\d{1,3}\.){3}\d{1,3}\b"
_IPV6 = r"\b(?:[A-Fa-f0-9]{1,4}:){2,7}[A-Fa-f0-9]{1,4}\b"
_API_KEY = (
    r"\b(?:sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16}|"
    r"xox[abp]-[A-Za-z0-9-]{10,}|AIza[A-Za-z0-9_-]{20,})\b"
)


@dataclass(frozen=True)
class DlpRule:
    """A single detection category: name + compiled-able regex source."""

    category: str
    pattern: str
    description: str = ""


# The built-in default set. ``credit_card`` is intentionally *not* listed: it is
# validated with Luhn in the redactor (a bare 13–19 digit run has too many false
# positives) and is always applied by DlpRuleSet.scan. ``cn_id`` IS listed — the
# QA P1-1 guarantee that a default deployment blocks Chinese resident IDs.
DEFAULT_RULES: tuple[DlpRule, ...] = (
    DlpRule("email", _EMAIL, "邮箱地址"),
    DlpRule("api_key", _API_KEY, "API 密钥 / 令牌"),
    DlpRule("cn_id", _CN_ID, "中国居民身份证号"),
    DlpRule("phone", _PHONE, "电话号码"),
    DlpRule("ssn", _SSN, "美国社会安全号"),
    DlpRule("ipv6", _IPV6, "IPv6 地址"),
    DlpRule("ipv4", _IPV4, "IPv4 地址"),
)

# Always applied by the redactor regardless of the configurable regex rules.
_ALWAYS_ON_CATEGORIES: tuple[str, ...] = ("credit_card",)


@dataclass(frozen=True)
class DlpRuleMatch:
    """A single redaction hit (mirrors ``RedactionMatch`` for a stable surface)."""

    category: str
    start: int
    end: int
    original: str


class DlpRuleSet:
    """An ordered collection of DLP categories with a fail-closed scanner."""

    def __init__(self, rules: Iterable[DlpRule]) -> None:
        self._rules: tuple[DlpRule, ...] = tuple(rules)
        # Compile once; a bad pattern is a hard error at construction time so a
        # typo'd rule file fails loudly instead of silently not scanning.
        self._compiled: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
            (rule.category, re.compile(rule.pattern)) for rule in self._rules
        )

    # -- constructors --------------------------------------------------------
    @classmethod
    def default(cls) -> "DlpRuleSet":
        """The built-in rule set (includes ``cn_id`` + Luhn ``credit_card``)."""
        return cls(DEFAULT_RULES)

    @classmethod
    def from_file(
        cls, path: str, *, base: "DlpRuleSet | None" = None
    ) -> "DlpRuleSet":
        """Load a JSON override, merged onto ``base`` (default: built-ins).

        Each entry in the file's ``rules`` array either *adds* a new category or
        *replaces* a built-in one (matched by ``category``). Categories absent
        from the file keep their default behaviour — the gate never loosens.
        """
        base_set = base if base is not None else cls.default()
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            logger.warning("DLP rules file not found: %s — using defaults", path)
            return base_set
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("DLP rules file unreadable (%s) — using defaults: %s", path, exc)
            return base_set

        raw_rules = payload.get("rules") if isinstance(payload, dict) else None
        if not isinstance(raw_rules, list):
            logger.warning("DLP rules file %s has no 'rules' array — using defaults", path)
            return base_set

        merged: dict[str, DlpRule] = {rule.category: rule for rule in base_set._rules}
        for entry in raw_rules:
            if not isinstance(entry, dict):
                continue
            category = str(entry.get("category", "")).strip()
            pattern = entry.get("pattern")
            if not category or not isinstance(pattern, str) or not pattern:
                continue
            merged[category] = DlpRule(
                category=category,
                pattern=pattern,
                description=str(entry.get("description", "")),
            )
        return cls(merged.values())

    # -- introspection -------------------------------------------------------
    @property
    def rules(self) -> tuple[DlpRule, ...]:
        return self._rules

    @property
    def categories(self) -> list[str]:
        """All active categories (configurable + always-on like credit_card)."""
        seen = [rule.category for rule in self._rules]
        for always in _ALWAYS_ON_CATEGORIES:
            if always not in seen:
                seen.append(always)
        return seen

    # -- scanning ------------------------------------------------------------
    def scan(self, text: str) -> tuple[str, list[DlpRuleMatch]]:
        """Redact ``text`` and return it plus the list of matches.

        Runs the hardened ``pii_redactor`` first (credit-card Luhn + built-ins),
        then applies every configured rule on the already-redacted text. Because
        built-in matches are already replaced with ``[REDACTED:<cat>]`` tokens,
        re-applying the same patterns is a no-op on them — they only add hits the
        base pass missed plus any custom categories.
        """
        from forgeflow.security.pii_redactor import redact

        redacted, base_matches = redact(text)
        matches: list[DlpRuleMatch] = [
            DlpRuleMatch(m.category, m.start, m.end, m.original) for m in base_matches
        ]

        current = redacted
        for category, pattern in self._compiled:
            current, found = _apply_pattern(current, pattern, category)
            matches.extend(found)

        return current, matches


def _apply_pattern(
    text: str, pattern: re.Pattern[str], category: str
) -> tuple[str, list[DlpRuleMatch]]:
    found: list[DlpRuleMatch] = []
    replacement = f"[REDACTED:{category}]"

    def _sub(match: re.Match[str]) -> str:
        found.append(
            DlpRuleMatch(category, match.start(), match.end(), match.group(0))
        )
        return replacement

    return pattern.sub(_sub, text), found


@lru_cache(maxsize=8)
def _load_cached(path: str) -> DlpRuleSet:
    return DlpRuleSet.from_file(path) if path else DlpRuleSet.default()


def load_rule_set(path: str | None = None) -> DlpRuleSet:
    """Return the rule set for ``path`` (cached). Empty/``None`` ⇒ built-ins."""
    return _load_cached(path or "")


def clear_rule_set_cache() -> None:
    """Test helper: drop the cached rule sets after mutating env/file."""
    _load_cached.cache_clear()


def rule_set_to_dict(rule_set: DlpRuleSet) -> dict[str, Any]:
    """Serialisable view of a rule set (for the security hub UI / audit)."""
    return {
        "categories": rule_set.categories,
        "rules": [
            {
                "category": rule.category,
                "pattern": rule.pattern,
                "description": rule.description,
            }
            for rule in rule_set.rules
        ],
    }
