"""INC46 T32 — PII scrubber (数据脱敏) for user-document content.

Red line 13: **user-document content must never enter an Experience (nor the
pattern miner that consumes experiences) before it has been scrubbed.** This
module is the single gate that enforces it.

Design (reuse-first, additive)
------------------------------
The battle-tested regexes already live in
:mod:`forgeflow.security.pii_redactor` (email / phone / ssn / cn_id /
credit-card-Luhn / ipv4 / ipv6 / api_key). We **reuse those patterns verbatim**
(``_pii._EMAIL_RE.pattern`` …) instead of writing a second regex set, and layer
the task's additional categories on top:

============  ==========================================================
category      detection
============  ==========================================================
name          label-anchored (姓名 / 联系人 / …) — high precision
phone         ``pii_redactor`` phone **+** mainland-CN mobile (``1[3-9]…``)
email         ``pii_redactor`` email
cn_id         ``pii_redactor`` Chinese resident ID
address       administrative-shape (省/市 + 区/县 + …路/号)
bank_card     ``pii_redactor`` credit-card pattern **+** Luhn validation
company       legal-suffix (有限公司 / 集团 / Inc. / Ltd. / …)
ssn           ``pii_redactor`` US SSN
============  ==========================================================

The one behavioural extension over :func:`pii_redactor.redact` is the **strategy
per category** (additive — ``redact`` stays the default):

* ``redact`` — a placeholder ``[REDACTED:<category>]`` (default, same token the
  legacy redactor emits);
* ``hash``   — ``[HASH:<category>:<hmac16>]`` where ``hmac16`` is
  ``HMAC-SHA256(master_secret, tenant_id)`` over the original value. It is
  **per-tenant** (the same value in two tenants hashes differently ⇒ cross-tenant
  unlinkable) and **deterministic within a tenant** (an idempotent re-scrub yields
  the same token). No secret is hard-coded — the key is derived from
  ``Settings.scrub_hash_secret`` (falling back to ``Settings.api_secret_key``);
* ``drop``   — remove the value entirely.

Availability (可用性)
--------------------
Scrubbing must not turn a document into garbage: its structure / numbering /
references must survive. :func:`check_availability` compares the multiset of
"reference tokens" (``[n]`` / ``(n)`` / ``#n`` / line-start numbered-list
markers) before and after; :meth:`Scrubber.scrub_document` **refuses** (raises
:class:`ScrubAvailabilityError`) rather than silently corrupt a reference.

Tenant discipline (红线 5): every entry point takes the owning tenant as its
**first** argument. A falsy tenant is fail-closed — reads return the empty set
and a ``hash``-strategy scrub refuses (a per-tenant salt needs a tenant).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from forgeflow.security import pii_redactor as _pii

logger = logging.getLogger(__name__)

__all__ = [
    # version / vocabulary
    "SCRUB_VERSION",
    "STRATEGY_REDACT",
    "STRATEGY_HASH",
    "STRATEGY_DROP",
    "STRATEGIES",
    "SCRUB_STATUS_SCRUBBED",
    "SCRUB_STATUS_REFUSED",
    # categories
    "CATEGORY_NAME",
    "CATEGORY_PHONE",
    "CATEGORY_EMAIL",
    "CATEGORY_CN_ID",
    "CATEGORY_ADDRESS",
    "CATEGORY_BANK_CARD",
    "CATEGORY_COMPANY",
    "CATEGORY_SSN",
    "DEFAULT_CATEGORIES",
    # value objects
    "ScrubRule",
    "ScrubMatch",
    "ScrubResult",
    "ScrubOutcome",
    "DEFAULT_RULES",
    # errors
    "ScrubError",
    "ScrubAvailabilityError",
    # engine
    "Scrubber",
    "get_scrubber",
    "set_scrubber",
    "reset_scrubber",
    "check_availability",
    "reference_tokens",
    # experience helpers
    "scrub_experience_record",
    "miner_eligible",
    "filter_miner_eligible",
    "mine_eligible_experiences",
]

# --------------------------------------------------------------------------- #
# Vocabulary                                                                   #
# --------------------------------------------------------------------------- #
#: The scrubber algorithm version stamped onto every scrubbed experience
#: (``experiences.scrub_version``). Bumping it makes a re-scrub *effective*.
SCRUB_VERSION = "1"

STRATEGY_REDACT = "redact"
STRATEGY_HASH = "hash"
STRATEGY_DROP = "drop"
STRATEGIES: tuple[str, ...] = (STRATEGY_REDACT, STRATEGY_HASH, STRATEGY_DROP)

#: ``experiences.scrub_status`` vocabulary. ``None`` (NULL) means **not scrubbed**
#: (未测量 ⇒ NULL, 红线 4) — such a row is NOT eligible for the miner.
SCRUB_STATUS_SCRUBBED = "scrubbed"
SCRUB_STATUS_REFUSED = "refused"

CATEGORY_NAME = "name"
CATEGORY_PHONE = "phone"
CATEGORY_EMAIL = "email"
CATEGORY_CN_ID = "cn_id"
CATEGORY_ADDRESS = "address"
CATEGORY_BANK_CARD = "bank_card"
CATEGORY_COMPANY = "company"
CATEGORY_SSN = "ssn"

#: The task's seven required categories plus ``ssn`` (a strong, low-false-positive
#: signature). ``ipv4`` / ``ipv6`` / ``api_key`` are deliberately **not** default
#: for experience content (an IP in a run summary can be legitimate operational
#: detail); pass them via ``enabled``/``rules`` to opt in.
DEFAULT_CATEGORIES: tuple[str, ...] = (
    CATEGORY_NAME,
    CATEGORY_PHONE,
    CATEGORY_EMAIL,
    CATEGORY_CN_ID,
    CATEGORY_ADDRESS,
    CATEGORY_BANK_CARD,
    CATEGORY_COMPANY,
    CATEGORY_SSN,
)

#: Categories whose match must additionally pass the Luhn checksum.
_LUHN_CATEGORIES = frozenset({CATEGORY_BANK_CARD, "credit_card"})

# --------------------------------------------------------------------------- #
# Patterns — the built-ins are imported from ``pii_redactor`` (single source).  #
# --------------------------------------------------------------------------- #
# Mainland-CN mobile: 11 digits, ``1`` + ``[3-9]`` + 9 digits. Not covered by the
# legacy US-shaped ``_PHONE_RE`` (it needs exactly 10 digits), so it is the one
# genuinely new phone pattern — a *category extension*, not a duplicate.
_CN_MOBILE_PATTERN = r"(?<!\d)1[3-9]\d{9}(?!\d)"

# 姓名 — label-anchored for precision (an unlabelled 2-4 char token is far too
# ambiguous to redact safely). The captured group (1) is the name itself.
_NAME_PATTERN = (
    r"(?:姓名|联系人|收件人|申请人|客户姓名|负责人|法定代表人|签字|签名)"
    r"\s*[：:]\s*([\u4e00-\u9fa5A-Za-z·]{2,8})"
)

# 地址 — requires the administrative shape (省/市 + 区/县 + …路/号) so an ordinary
# phrase like "华东地区" is never mistaken for an address.
_ADDRESS_PATTERN = (
    r"[\u4e00-\u9fa5]{2,10}(?:省|自治区|市)"
    r"[\u4e00-\u9fa5]{1,10}(?:区|县|市|旗)"
    r"[\u4e00-\u9fa5A-Za-z0-9]{1,30}(?:路|街|道|巷|号)"
)

# 公司名 — anchored on a legal suffix so a bare "公司" or "科技" is not redacted.
_COMPANY_PATTERN = (
    r"[\u4e00-\u9fa5A-Za-z0-9]{2,30}"
    r"(?:股份有限公司|有限责任公司|有限公司|集团有限公司|控股有限公司|集团|"
    r"Inc\.?|Corp\.?|Ltd\.?|LLC)"
)


@dataclass(frozen=True)
class ScrubRule:
    """One detection rule: a category, a regex source and the capture group.

    ``group == 0`` replaces the whole match; ``group == n`` replaces only the
    ``n``-th capture group (used by the label-anchored ``name`` rule so the label
    itself is preserved and only the name is masked).
    """

    category: str
    pattern: str
    group: int = 0
    description: str = ""


#: The default rule set. Built-in regexes are the ``pii_redactor`` patterns —
#: referenced through their compiled ``.pattern`` source so there is exactly one
#: definition of every built-in category.
DEFAULT_RULES: tuple[ScrubRule, ...] = (
    ScrubRule(CATEGORY_EMAIL, _pii._EMAIL_RE.pattern, 0, "邮箱地址"),
    ScrubRule(CATEGORY_CN_ID, _pii._CN_ID_RE.pattern, 0, "中国居民身份证号"),
    ScrubRule(CATEGORY_BANK_CARD, _pii._CREDIT_CARD_RE.pattern, 0, "银行卡号 (Luhn 校验)"),
    ScrubRule(CATEGORY_SSN, _pii._SSN_RE.pattern, 0, "美国社会安全号"),
    ScrubRule(CATEGORY_PHONE, _pii._PHONE_RE.pattern, 0, "电话号码"),
    ScrubRule(CATEGORY_PHONE, _CN_MOBILE_PATTERN, 0, "中国大陆手机号"),
    ScrubRule(CATEGORY_NAME, _NAME_PATTERN, 1, "姓名 (标签锚定)"),
    ScrubRule(CATEGORY_ADDRESS, _ADDRESS_PATTERN, 0, "地址 (行政区划形态)"),
    ScrubRule(CATEGORY_COMPANY, _COMPANY_PATTERN, 0, "公司名 (法定后缀)"),
)


# --------------------------------------------------------------------------- #
# Value objects                                                                #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ScrubMatch:
    """A single scrubbed span (position is relative to the *original* text)."""

    category: str
    start: int
    end: int
    original: str
    strategy: str
    replacement: str


@dataclass
class ScrubResult:
    """The outcome of scrubbing one string."""

    original: str
    text: str
    matches: list[ScrubMatch] = field(default_factory=list)
    version: str = SCRUB_VERSION
    strategy_by_category: dict[str, str] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        """Whether the scrub altered the text at all."""
        return self.text != self.original

    @property
    def categories(self) -> list[str]:
        """Sorted, de-duplicated categories that were hit."""
        return sorted({m.category for m in self.matches})

    @property
    def match_count(self) -> int:
        return len(self.matches)

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed": self.changed,
            "categories": self.categories,
            "match_count": self.match_count,
            "version": self.version,
            "text": self.text,
        }


@dataclass
class ScrubOutcome:
    """The outcome of scrubbing one experience record."""

    record: Any
    changed: bool
    categories: list[str] = field(default_factory=list)
    match_count: int = 0
    from_version: str | None = None
    to_version: str = SCRUB_VERSION
    refused: str | None = None
    content_sha256: str = ""
    #: Whether the free text itself changed (PII was actually removed) as opposed
    #: to only the bookkeeping columns being stamped. Drives the embedding
    #: recompute so a PII-free save keeps its caller-supplied vector untouched.
    text_changed: bool = False

    @property
    def ok(self) -> bool:
        """Whether the scrub completed (not refused)."""
        return self.refused is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed": self.changed,
            "text_changed": self.text_changed,
            "categories": list(self.categories),
            "match_count": self.match_count,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "refused": self.refused,
            "content_sha256": self.content_sha256,
        }


# --------------------------------------------------------------------------- #
# Errors                                                                       #
# --------------------------------------------------------------------------- #
class ScrubError(Exception):
    """Base class for scrubber failures."""


class ScrubAvailabilityError(ScrubError):
    """Scrubbing would have destroyed a document reference / numbering token.

    Fail-closed (禁止静默通过): the caller must refuse to persist the corrupted
    document rather than let a broken artefact through.
    """

    def __init__(self, missing: list[str], original: str, scrubbed: str) -> None:
        self.missing = list(missing)
        self.original = original
        self.scrubbed = scrubbed
        super().__init__(
            "scrub would break document structure — missing reference token(s): "
            + ", ".join(repr(t) for t in self.missing)
        )


# --------------------------------------------------------------------------- #
# Availability (structure / numbering / reference preservation)                #
# --------------------------------------------------------------------------- #
#: Tokens that encode a document's internal references / numbering. They must be
#: byte-identical before and after scrubbing.
_REFERENCE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\[\s*\d+\s*\]"),
    re.compile(r"\(\s*\d+\s*\)"),
    re.compile(r"(?<![\w])#\d+"),
    re.compile(r"(?m)^[ \t]*\d+[.)][ \t]"),
)


def reference_tokens(text: str) -> list[str]:
    """Extract the document's reference / numbering tokens (order preserved)."""
    tokens: list[str] = []
    for pattern in _REFERENCE_PATTERNS:
        tokens.extend(m.group(0) for m in pattern.finditer(text or ""))
    return tokens


def check_availability(original: str, scrubbed: str) -> list[str]:
    """Return the reference tokens present in ``original`` but lost in ``scrubbed``.

    An empty list means the document's numbering / references survived intact.
    """
    from collections import Counter

    before = Counter(reference_tokens(original))
    after = Counter(reference_tokens(scrubbed))
    missing: list[str] = []
    for token, count in before.items():
        lost = count - after.get(token, 0)
        missing.extend([token] * max(0, lost))
    return missing


# --------------------------------------------------------------------------- #
# The scrubber                                                                 #
# --------------------------------------------------------------------------- #
def _tenant_salt(secret: str, tenant_id: str) -> bytes:
    """Derive a **per-tenant** HMAC key from the master secret + tenant id.

    The tenant id is opaque and never collapsed, so two tenants never share a
    salt (红线 5) and the same value hashes differently across tenants.
    """
    return f"{secret}:{tenant_id}".encode("utf-8")


class Scrubber:
    """Configurable PII scrubber with per-category strategies.

    Args:
        rules: detection rules (default :data:`DEFAULT_RULES`).
        enabled: categories to apply (default :data:`DEFAULT_CATEGORIES`).
        strategies: category → strategy (default: every category ``redact``).
        preserve_structure: when ``True`` (default), :meth:`scrub_document` runs
            :func:`check_availability` and refuses a structure-breaking scrub.
        settings: optional injected ``Settings`` (tests / explicitness).
    """

    def __init__(
        self,
        *,
        rules: Sequence[ScrubRule] | None = None,
        enabled: Iterable[str] | None = None,
        strategies: dict[str, str] | None = None,
        preserve_structure: bool = True,
        settings: Any | None = None,
    ) -> None:
        self._rules: tuple[ScrubRule, ...] = tuple(rules if rules is not None else DEFAULT_RULES)
        self._compiled: tuple[re.Pattern[str], ...] = tuple(
            re.compile(rule.pattern) for rule in self._rules
        )
        self._enabled: frozenset[str] = frozenset(
            enabled if enabled is not None else DEFAULT_CATEGORIES
        )
        self._strategies: dict[str, str] = {
            str(k): str(v).lower() for k, v in (strategies or {}).items()
        }
        for category, strategy in self._strategies.items():
            if strategy not in STRATEGIES:
                raise ValueError(
                    f"unknown scrub strategy {strategy!r} for {category!r}; "
                    f"expected one of {STRATEGIES}"
                )
        self._preserve_structure = bool(preserve_structure)
        self._settings = settings

    # -- configuration ------------------------------------------------------- #
    @property
    def rules(self) -> tuple[ScrubRule, ...]:
        return self._rules

    @property
    def enabled(self) -> frozenset[str]:
        return self._enabled

    @property
    def preserve_structure(self) -> bool:
        return self._preserve_structure

    def strategy_for(self, category: str) -> str:
        """The strategy for ``category`` (default :data:`STRATEGY_REDACT`)."""
        return self._strategies.get(category, STRATEGY_REDACT)

    def _secret(self) -> str:
        if self._settings is None:
            from forgeflow.config import get_settings

            self._settings = get_settings()
        configured = getattr(self._settings, "scrub_hash_secret", None)
        value = ""
        if configured is not None:
            value = (
                configured.get_secret_value()
                if hasattr(configured, "get_secret_value")
                else str(configured)
            )
        if not value:
            api_key = getattr(self._settings, "api_secret_key", None)
            value = (
                api_key.get_secret_value()
                if hasattr(api_key, "get_secret_value")
                else str(api_key or "")
            )
        if not value:
            raise ScrubError("no scrub secret available (set SCRUB_HASH_SECRET) — fail closed")
        return value

    # -- span collection ----------------------------------------------------- #
    def _raw_matches(
        self, text: str
    ) -> list[tuple[int, int, str, int, str]]:
        """Collect every candidate span as ``(start, end, category, rule_idx, value)``."""
        raw: list[tuple[int, int, str, int, str]] = []
        for index, rule in enumerate(self._rules):
            if rule.category not in self._enabled:
                continue
            pattern = self._compiled[index]
            for match in pattern.finditer(text):
                if rule.group:
                    if match.group(rule.group) is None:
                        continue
                    start, end = match.span(rule.group)
                else:
                    start, end = match.span()
                if start == end:
                    continue
                value = text[start:end]
                if rule.category in _LUHN_CATEGORIES and not _pii._luhn_valid(
                    re.sub(r"[ -]", "", value)
                ):
                    continue
                raw.append((start, end, rule.category, index, value))
        return raw

    @staticmethod
    def _resolve_overlaps(
        raw: list[tuple[int, int, str, int, str]]
    ) -> list[tuple[int, int, str, str]]:
        """Greedy left-to-right, longest-match-wins overlap resolution."""
        raw.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[3]))
        accepted: list[tuple[int, int, str, str]] = []
        last_end = -1
        for start, end, category, _index, value in raw:
            if start >= last_end:
                accepted.append((start, end, category, value))
                last_end = end
        return accepted

    # -- rendering ----------------------------------------------------------- #
    def _replacement(self, category: str, value: str, tenant_id: str | None) -> str:
        strategy = self.strategy_for(category)
        if strategy == STRATEGY_REDACT:
            return f"[REDACTED:{category}]"
        if strategy == STRATEGY_DROP:
            return ""
        # hash (HMAC + per-tenant salt)
        if not tenant_id:
            raise ScrubError(
                "hash strategy requires a tenant id (per-tenant salt) — fail closed"
            )
        digest = hmac.new(
            _tenant_salt(self._secret(), str(tenant_id)),
            value.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:16]
        return f"[HASH:{category}:{digest}]"

    def scrub(self, text: str, *, tenant_id: str | None = None) -> ScrubResult:
        """Scrub ``text``. No availability guard — see :meth:`scrub_document`."""
        if not text:
            return ScrubResult(original=text or "", text=text or "", version=SCRUB_VERSION)

        accepted = self._resolve_overlaps(self._raw_matches(text))
        chunks: list[str] = []
        matches: list[ScrubMatch] = []
        cursor = 0
        for start, end, category, value in accepted:
            chunks.append(text[cursor:start])
            replacement = self._replacement(category, value, tenant_id)
            chunks.append(replacement)
            matches.append(
                ScrubMatch(
                    category=category,
                    start=start,
                    end=end,
                    original=value,
                    strategy=self.strategy_for(category),
                    replacement=replacement,
                )
            )
            cursor = end
        chunks.append(text[cursor:])

        return ScrubResult(
            original=text,
            text="".join(chunks),
            matches=matches,
            version=SCRUB_VERSION,
            strategy_by_category={c: self.strategy_for(c) for c in self._enabled},
        )

    def detect(self, text: str) -> list[ScrubMatch]:
        """Detect spans without persisting a strategy effect (residual check)."""
        result = self.scrub(text)
        return result.matches

    def scrub_document(self, text: str, *, tenant_id: str | None = None) -> ScrubResult:
        """Scrub a document, refusing (raising) if it would break references."""
        result = self.scrub(text, tenant_id=tenant_id)
        if self._preserve_structure:
            missing = check_availability(result.original, result.text)
            if missing:
                raise ScrubAvailabilityError(missing, result.original, result.text)
        return result


# --------------------------------------------------------------------------- #
# Process-wide scrubber                                                        #
# --------------------------------------------------------------------------- #
_SCRUBBER: Scrubber | None = None


def get_scrubber() -> Scrubber:
    """Return the process-wide scrubber (built on first use)."""
    global _SCRUBBER
    if _SCRUBBER is None:
        _SCRUBBER = Scrubber()
    return _SCRUBBER


def set_scrubber(scrubber: Scrubber | None) -> None:
    """Test helper — pin an explicit scrubber."""
    global _SCRUBBER
    _SCRUBBER = scrubber


def reset_scrubber() -> None:
    """Test helper — drop the cached scrubber (next call rebuilds it)."""
    global _SCRUBBER
    _SCRUBBER = None


# --------------------------------------------------------------------------- #
# Experience helpers — the write-before-scrub + the miner admission gate        #
# --------------------------------------------------------------------------- #
#: The record fields that carry free-text (user-document-derived) content.
def _decision_texts(decisions: Any) -> list[str]:
    """Every string value inside a ``decisions`` structure (list of dicts)."""
    texts: list[str] = []
    for decision in decisions or []:
        if isinstance(decision, dict):
            for key in ("reason", "detail", "decision", "note", "text"):
                value = decision.get(key)
                if isinstance(value, str) and value:
                    texts.append(value)
        elif isinstance(decision, str) and decision:
            texts.append(decision)
    return texts


def scrub_experience_record(
    record: Any,
    *,
    tenant_id: str | None = None,
    version: str = SCRUB_VERSION,
    scrubber: Scrubber | None = None,
) -> ScrubOutcome:
    """Scrub an experience record's free-text fields **in place** and stamp it.

    Scrubs ``summary``, ``reusable_steps[].note`` and every string value in
    ``decisions``. On success it stamps ``scrub_status='scrubbed'`` +
    ``scrub_version`` (both fields are additive; migration ``026``). On a
    structure-breaking field it leaves the record untouched, does **not** stamp it
    and returns ``refused`` (fail-closed, 禁止静默通过).
    """
    scrubber = scrubber or get_scrubber()
    tenant = tenant_id if tenant_id is not None else getattr(record, "tenant_id", None)

    from_version = getattr(record, "scrub_version", None)
    from_status = getattr(record, "scrub_status", None)

    categories: set[str] = set()
    matches_total = 0
    content_parts: list[str] = []

    def _scrub_field(value: str) -> tuple[str, bool]:
        nonlocal matches_total
        result = scrubber.scrub_document(value, tenant_id=tenant)
        matches_total += result.match_count
        categories.update(result.categories)
        return result.text, result.changed

    changed_text = False
    try:
        summary = getattr(record, "summary", "") or ""
        scrubbed_summary, summary_changed = _scrub_field(summary)
        content_parts.append(scrubbed_summary)

        steps = getattr(record, "reusable_steps", None) or []
        for step in steps:
            if isinstance(step, dict):
                note = step.get("note", "")
                if isinstance(note, str) and note:
                    scrubbed_note, note_changed = _scrub_field(note)
                    content_parts.append(scrubbed_note)
                    if note_changed:
                        step["note"] = scrubbed_note
                        changed_text = True

        decisions = getattr(record, "decisions", None) or []
        rebuilt: list[Any] = []
        decisions_changed = False
        for decision in decisions:
            if isinstance(decision, dict):
                new_decision = dict(decision)
                for key in ("reason", "detail", "decision", "note", "text"):
                    value = new_decision.get(key)
                    if isinstance(value, str) and value:
                        scrubbed_value, value_changed = _scrub_field(value)
                        content_parts.append(scrubbed_value)
                        if value_changed:
                            new_decision[key] = scrubbed_value
                            decisions_changed = True
                rebuilt.append(new_decision)
            elif isinstance(decision, str) and decision:
                scrubbed_value, value_changed = _scrub_field(decision)
                content_parts.append(scrubbed_value)
                if value_changed:
                    decisions_changed = True
                rebuilt.append(scrubbed_value)
            else:
                rebuilt.append(decision)
        if decisions_changed:
            changed_text = True
    except ScrubAvailabilityError as exc:
        logger.warning("scrub refused | experience=%s: %s", getattr(record, "id", "?"), exc)
        return ScrubOutcome(
            record=record,
            changed=False,
            from_version=from_version,
            to_version=version,
            refused=str(exc),
        )

    if summary_changed:
        record.summary = scrubbed_summary
        changed_text = True
    if decisions_changed:
        record.decisions = rebuilt

    record.scrub_status = SCRUB_STATUS_SCRUBBED
    record.scrub_version = version

    content_sha256 = hashlib.sha256("\n".join(content_parts).encode("utf-8")).hexdigest()
    changed = bool(
        changed_text or from_version != version or from_status != SCRUB_STATUS_SCRUBBED
    )
    return ScrubOutcome(
        record=record,
        changed=changed,
        categories=sorted(categories),
        match_count=matches_total,
        from_version=from_version,
        to_version=version,
        content_sha256=content_sha256,
        text_changed=bool(changed_text),
    )


def miner_eligible(record: Any) -> bool:
    """Whether ``record`` may be read by the pattern miner.

    Admission rule (红线 13): the experience must carry a valid ``scrub_status``
    (``'scrubbed'``) **and** a ``scrub_version``. A ``NULL`` ``scrub_status``
    (never scrubbed) is **not** eligible — the miner must not read it.
    """
    status = getattr(record, "scrub_status", None)
    version = getattr(record, "scrub_version", None)
    return bool(status == SCRUB_STATUS_SCRUBBED and version)


def filter_miner_eligible(records: Iterable[Any]) -> list[Any]:
    """Keep only the records admitted by :func:`miner_eligible` (order kept)."""
    return [record for record in records if miner_eligible(record)]


def _experience_runs(records: Iterable[Any]) -> list[list[dict[str, Any]]]:
    """Project experiences into the miner's run shape (mirrors the compiler)."""
    runs: list[list[dict[str, Any]]] = []
    for record in records:
        outcome = str(getattr(record, "outcome", "success") or "success")
        status = "ok" if outcome == "success" else "error"
        run_id = str(getattr(record, "run_id", "") or "")
        sequence: list[dict[str, Any]] = []
        for index, step in enumerate(getattr(record, "reusable_steps", None) or []):
            if not isinstance(step, dict):
                continue
            tool = step.get("tool")
            if not tool:
                continue
            raw_step: dict[str, Any] = {
                "tool": str(tool),
                "run_id": run_id,
                "status": status,
                "step_index": index,
            }
            out = step.get("output") or step.get("output_data")
            if isinstance(out, dict):
                raw_step["output"] = {"payload": out}
            sequence.append(raw_step)
        runs.append(sequence)
    return runs


async def mine_eligible_experiences(
    tenant_id: str | None,
    *,
    repo: Any | None = None,
    min_support: int | None = None,
    limit: int = 500,
) -> list[Any]:
    """Mine tenant patterns **only** over scrub-eligible experiences.

    Tenant fail-closed: an unresolved tenant reads nothing (``[]``). The
    admission gate is :func:`miner_eligible` — an experience whose
    ``scrub_status`` is ``NULL`` is invisible to the miner.
    """
    if not tenant_id:
        return []

    if repo is None:
        from forgeflow.repositories import get_experience_repository

        repo = get_experience_repository()

    records = await repo.list(tenant_id, limit=limit)
    eligible = filter_miner_eligible(records)

    from forgeflow.skills.pattern_miner import mine_patterns

    if min_support is None:
        from forgeflow.config import get_settings

        min_support = int(get_settings().skill_candidate_min_experiences)
    return mine_patterns(_experience_runs(eligible), min_support=min_support)
