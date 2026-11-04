"""Deterministic content grading (8-feature program, Phase 5).

Grades a proposed tool action (pre-execution) and a tool output
(post-execution) with deterministic rules only — never the acting
model grading its own actions (OpenHands PR discussion + LLM-judge
bias literature: an agent about to take a risky action is unlikely
to grade that action risky). An independent classifier model is a
later step; v1 is rules, because rules are auditable, fast, and have
no model-switch latency or Chinese/code precision unknowns.

Levels mirror OpenHands' ``SecurityRisk``: LOW / MEDIUM / HIGH /
UNKNOWN. UNKNOWN is not comparable and never defaults to LOW:
a grader failure suspends (enforce) or records (shadow), never
silently passes.

Vocabulary priority (ForgeFlow is a coding agent, not a chatbot):
secrets/credentials, destructive ops, data egress, injected
instructions — in that order. Toxicity classifiers are deliberately
absent: they judge chat content, not whether a diff deletes a
database.

Two asymmetries are principled, not omissions:

* Destructive/egress rules apply ONLY to action tools
  (``code.execute`` / ``code.run`` / ``agent.delegate``). A search
  query mentioning ``rm -rf`` is data, not an action; grading it
  would flag every tutorial lookup.
* Output grading applies ONLY secret + injection rules. Output text
  does not act; a destructive command quoted in output is caught
  when the model actually proposes it (pre-grade of the next round).

History window: the pre-grade also sees the categories of the last
20 records in the run. A secret observed earlier + an egress action
now is an exfiltration shape even when each half looks benign alone
(sequence-level grading, the ToolShield motivation).

Mode (``CONTENT_GRADE_MODE`` env, default ``shadow``):

* ``shadow`` — record the grade on the invocation + log, never
  change the outcome. The research prescribes a noop shadow period
  to calibrate false positives before any enforcement: coding agents
  legitimately ``rm`` files, run ``DROP`` in test DBs, and fetch
  URLs, so enforcing uncalibrated rules would end in approval
  fatigue and a disabled gate.
* ``enforce`` — HIGH / UNKNOWN pre-grades refuse (a verified Phase 4
  approval supersedes a refusal: the owner already reviewed that
  exact call); MEDIUM pre-grades allow with a recorded warning;
  post-grades always record only (the tool already ran — the
  defense there is the next round's history window).
* An explicit but unknown mode value fails closed to ``enforce``
  (a typo must not silently shadow); unset means ``shadow``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass

from forgeflow.security.prompt_guard import RiskLevel, scan_prompt

logger = logging.getLogger(__name__)

__all__ = [
    "ACTION_TOOLS",
    "Grade",
    "calibration_report",
    "disposition",
    "get_grade_mode",
    "grade_action",
    "grade_action_with_packs",
    "grade_output",
    "grade_output_with_packs",
    "reset_grade_mode",
    "set_grade_mode",
]

#: Tools whose arguments ARE actions (not data about actions).
ACTION_TOOLS: frozenset[str] = frozenset({"code.execute", "code.run", "agent.delegate"})

#: History window for the sequence rule (ToolShield's cited window).
HISTORY_WINDOW = 20

#: Grading text cap: regex over unbounded payloads is a DoS vector.
MAX_GRADE_CHARS = 200_000

#: Levels, ordered for aggregation (UNKNOWN is handled separately).
_LEVEL_ORDER = {"low": 0, "medium": 1, "high": 2}

_PLACEHOLDERS = frozenset(
    {"xxx", "test", "example", "demo", "changeme", "placeholder", "your-key-here", "***", "todo"}
)


def _is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return (
        lowered in _PLACEHOLDERS
        or lowered.startswith("<")
        or lowered.startswith(("test", "example", "demo", "fake"))
        or len(set(lowered)) <= 1
    )


# --- secret rules (HIGH, all tools, action + output) ----------------------- #

_SECRET_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("private key block", re.compile(r"-----BEGIN (?:[A-Z ]*)PRIVATE KEY-----")),
    ("aws access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    (
        "github token",
        re.compile(r"(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}"),
    ),
    ("slack token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("generic api key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
]


def _secret_reasons(text: str) -> list[str]:
    reasons: list[str] = []
    for name, pattern in _SECRET_RULES:
        if pattern.search(text):
            reasons.append(f"secret pattern: {name}")
    for match in re.finditer(
        r"(?i)(password|passwd|pwd|api[_-]?key|secret|token)\s*[:=]\s*[\"']?([^\s\"',;]{8,})",
        text,
    ):
        if not _is_placeholder(match.group(2)):
            reasons.append(f"secret assignment: {match.group(1).lower()}")
    return reasons


# --- destructive / egress rules (HIGH/MEDIUM, action tools only) ----------- #

_DESTRUCTIVE_HIGH: list[tuple[str, re.Pattern[str]]] = [
    # Root/home targets only: "/" or "~" alone, "~/*", "/*" — "/tmp/x"
    # must NOT match (the "/" there is a path prefix, not the target).
    # The graded text is canonical JSON, so the target is followed by
    # a quote, not whitespace: match "/" or "~" NOT followed by a word
    # character or another "/" (with a separate "~/*" alternative).
    (
        "rm -rf root/home",
        re.compile(r"rm\s+-[a-z]*r[a-z]*f\s+(?:/(?![\w/])|~(?![\w/])|~/\*(?![\w/]))"),
    ),
    ("mkfs", re.compile(r"\bmkfs\b")),
    ("dd to device", re.compile(r"\bdd\b.*\bof=/dev/")),
    ("drop database", re.compile(r"\bDROP\s+DATABASE\b", re.IGNORECASE)),
    ("fork bomb", re.compile(r":\(\)\{\s*:\|:&\s*\};:")),
    ("pipe to shell", re.compile(r"\b(?:curl|wget)\b[^|]*\|\s*(?:sh|bash)\b")),
]

_DESTRUCTIVE_MEDIUM: list[tuple[str, re.Pattern[str]]] = [
    ("rm -rf", re.compile(r"rm\s+-[a-z]*r[a-z]*f\b")),
    ("drop table", re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE)),
    ("delete from", re.compile(r"\bDELETE\s+FROM\b", re.IGNORECASE)),
    ("truncate table", re.compile(r"\bTRUNCATE\s+TABLE\b", re.IGNORECASE)),
    ("chmod 777 recursive", re.compile(r"chmod\s+-R\s+777\b")),
    ("shutdown/reboot", re.compile(r"\b(?:shutdown|reboot|poweroff)\b")),
]

_EGRESS_MEDIUM: list[tuple[str, re.Pattern[str]]] = [
    ("curl", re.compile(r"\bcurl\b")),
    ("wget", re.compile(r"\bwget\b")),
    ("scp", re.compile(r"\bscp\b")),
    ("rsync to remote", re.compile(r"\brsync\b.*:")),
    ("http client post", re.compile(r"\brequests\.(?:post|put)\s*\(")),
    ("urllib urlopen", re.compile(r"urllib\.request\.urlopen")),
    ("netcat/telnet", re.compile(r"\b(?:nc|telnet)\s+")),
]


@dataclass(frozen=True)
class Grade:
    """One grading verdict (pure data; safe to persist on a record)."""

    level: str  # "low" | "medium" | "high" | "unknown"
    categories: frozenset[str] = frozenset()
    reasons: tuple[str, ...] = ()
    text_hash: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "level": self.level,
            "categories": sorted(self.categories),
            "reasons": list(self.reasons),
            "text_hash": self.text_hash,
        }


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _text_of(value: object) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str, ensure_ascii=False, sort_keys=True)
    except Exception:  # noqa: BLE001 — grading must never raise on weird input
        return repr(value)


def _aggregate(levels: list[str], categories: set[str], reasons: list[str], text_hash: str) -> Grade:
    level = "low"
    for candidate in levels:
        if _LEVEL_ORDER.get(candidate, 0) > _LEVEL_ORDER[level]:
            level = candidate
    return Grade(
        level=level,
        categories=frozenset(categories),
        reasons=tuple(dict.fromkeys(reasons))[:10],
        text_hash=text_hash,
    )


def _injection_grade(text: str) -> tuple[str, list[str]]:
    """Map the existing injection scanner onto grade levels."""
    try:
        score = scan_prompt(text)
    except Exception as exc:  # noqa: BLE001 — scanner failure is UNKNOWN, never LOW
        raise RuntimeError(f"injection scan failed: {exc}") from exc
    if score.level == RiskLevel.HIGH:
        return "high", [f"injection: {reason}" for reason in score.reasons]
    if score.level == RiskLevel.MEDIUM:
        return "medium", [f"injection: {reason}" for reason in score.reasons]
    return "low", []


def grade_action(
    tool: str, args: dict[str, object], history_categories: list[set[str]] | None = None
) -> Grade:
    """Grade a proposed action (pre-execution).

    ``history_categories`` is the list of category sets from the run's
    recent records (pre ∪ post); the sequence rule upgrades a lone
    egress action to HIGH when secret material appeared earlier.
    """
    try:
        return _grade_action(tool, args, history_categories or [])
    except Exception as exc:  # noqa: BLE001 — fail closed, never silent LOW
        logger.warning("content grade failed, UNKNOWN (fail-closed) | tool=%s err=%s", tool, exc)
        return Grade(level="unknown", categories=frozenset({"unknown"}), reasons=(f"grader failed: {exc}",))


def _grade_action(
    tool: str, args: dict[str, object], history_categories: list[set[str]]
) -> Grade:
    text = _text_of(args)[:MAX_GRADE_CHARS]
    text_hash = _hash(text)
    levels: list[str] = []
    categories: set[str] = set()
    reasons: list[str] = []

    secret = _secret_reasons(text)
    if secret:
        levels.append("high")
        categories.add("secret")
        reasons.extend(secret)

    if tool in ACTION_TOOLS:
        for name, pattern in _DESTRUCTIVE_HIGH:
            if pattern.search(text):
                levels.append("high")
                categories.add("destructive")
                reasons.append(f"destructive pattern: {name}")
        for name, pattern in _DESTRUCTIVE_MEDIUM:
            if pattern.search(text):
                levels.append("medium")
                categories.add("destructive")
                reasons.append(f"destructive pattern: {name}")
        for name, pattern in _EGRESS_MEDIUM:
            if pattern.search(text):
                levels.append("medium")
                categories.add("egress")
                reasons.append(f"egress pattern: {name}")
        # Pipe-to-shell is both egress and execution: HIGH, both categories.
        if re.search(r"\b(?:curl|wget)\b[^|]*\|\s*(?:sh|bash)\b", text):
            levels.append("high")
            categories.add("egress")
            reasons.append("egress pattern: pipe to shell")

    injection_level, injection_reasons = _injection_grade(text)
    if injection_level != "low":
        levels.append(injection_level)
        categories.add("injection")
        reasons.extend(injection_reasons)

    # Sequence rule: secret seen earlier + egress now = exfiltration shape.
    if "egress" in categories and any("secret" in cats for cats in history_categories):
        levels.append("high")
        reasons.append("sequence: secret material earlier in run + egress action now")

    return _aggregate(levels, categories, reasons, text_hash)


# --- pack B: independent structural / behavioural signals ------------------
#
# Phase 9 B4 — a SECOND rule pack, deliberately independent of pack A
# (Phase 5 vocabulary: token formats, destructive verbs, egress verbs).
# Pack B reads SHAPE, not vocabulary: encoded blobs, shell chains,
# privilege escalation, traversal-plus-write, and a generic
# credential-assignment-plus-egress shape. The two packs share no
# pattern: a regex bug or vocabulary miss in one cannot blind the
# other. A classifier model is deliberately NOT the second opinion
# here (precision in Chinese/code scenarios is unverified) — the
# second pack is deterministic, like the first.
_B64_RUN = re.compile(r"[A-Za-z0-9+/]{100,}={0,2}")
_HEX_RUN = re.compile(r"\b[0-9a-fA-F]{64,}\b")
_UNICODE_ESCAPES = re.compile(r"(?:\\u[0-9a-fA-F]{4}){3,}")
_DESTRUCTIVE_VERBS = re.compile(r"\b(?:rm|mkfs|dd|shred)\b")
_NETWORK_VERBS = re.compile(r"\b(?:curl|wget|nc|ncat|ssh)\b")
_SHELL_CONNECTORS = re.compile(r"&&|\|\||;|\|")
_PRIVILEGE = re.compile(r"\b(?:sudo|su)\b")
_CHMOD_777 = re.compile(r"chmod\s+777\b")
_TRAVERSAL = re.compile(r"(?:\.\./){3,}")
_WRITE_VERBS = re.compile(r"\b(?:write|save|create|tee)\b|>")
_GENERIC_CREDENTIAL = re.compile(
    r"\b(?:key|token|secret|password)\s*[=:]\s*[\"']([^\"'\s]{8,})[\"']", re.IGNORECASE
)
_EGRESS_VERBS = re.compile(r"\b(?:curl|wget|nc|ncat|ssh|send|upload|post)\b")


def _grade_pack_b(tool: str, text: str) -> tuple[str, set[str], list[str]]:
    """Independent pack-B verdict: (level, categories, reasons)."""
    levels: list[str] = []
    categories: set[str] = set()
    reasons: list[str] = []

    def _hit(level: str, category: str, reason: str) -> None:
        levels.append(level)
        categories.add(category)
        reasons.append(reason)

    # Obfuscation applies everywhere (action or output): a long encoded
    # blob is suspicious even quoted in output.
    b64 = _B64_RUN.search(text)
    if b64 and any(c.isdigit() for c in b64.group(0)) and any(c.isupper() for c in b64.group(0)):
        _hit("medium", "obfuscation", "obfuscation: long base64-like blob")
    if _HEX_RUN.search(text):
        _hit("medium", "obfuscation", "obfuscation: long hex blob")
    if _UNICODE_ESCAPES.search(text):
        _hit("medium", "obfuscation", "obfuscation: repeated unicode escapes")
    if tool not in ACTION_TOOLS:
        level = "low"
        for candidate in levels:
            if _LEVEL_ORDER.get(candidate, 0) > _LEVEL_ORDER[level]:
                level = candidate
        return level, categories, reasons

    if (
        _DESTRUCTIVE_VERBS.search(text)
        and _SHELL_CONNECTORS.search(text)
        and _NETWORK_VERBS.search(text)
    ):
        _hit("high", "shell_chain", "shell chain: destructive verb + connector + network verb")
    if _PRIVILEGE.search(text) and _DESTRUCTIVE_VERBS.search(text):
        _hit("medium", "privilege", "privilege: sudo/su combined with a destructive verb")
    if _CHMOD_777.search(text):
        _hit("medium", "privilege", "privilege: chmod 777")
    if _TRAVERSAL.search(text) and _WRITE_VERBS.search(text):
        _hit("medium", "traversal", "traversal: deep ../ chain combined with a write")
    cred = _GENERIC_CREDENTIAL.search(text)
    if cred and not _is_placeholder(cred.group(1)) and _EGRESS_VERBS.search(text):
        _hit("high", "credential_egress", "credential shape + egress verb in one action")
    level = "low"
    for candidate in levels:
        if _LEVEL_ORDER.get(candidate, 0) > _LEVEL_ORDER[level]:
            level = candidate
    return level, categories, reasons


def _combine_levels(a: str, b: str) -> str:
    """Fail-closed combination: UNKNOWN in either pack wins, else max."""
    if "unknown" in (a, b):
        return "unknown"
    return a if _LEVEL_ORDER.get(a, 0) >= _LEVEL_ORDER.get(b, 0) else b


def _with_packs(pack_a: Grade, pack_b_level: str, pack_b_categories: set[str], pack_b_reasons: list[str]) -> tuple[Grade, dict[str, object]]:
    """Combine pack A (a Grade) with a pack-B verdict.

    Returns (combined Grade, packs dict). The combined level is the
    fail-closed max; disagreement is recorded, never resolved by
    averaging — the executor logs it (calibration data for the
    shadow period) and the stricter level governs.
    """
    level = _combine_levels(pack_a.level, pack_b_level)
    categories = set(pack_a.categories) | set(pack_b_categories)
    reasons = list(dict.fromkeys([*pack_a.reasons, *pack_b_reasons]))[:10]
    combined = Grade(level=level, categories=frozenset(categories), reasons=tuple(reasons), text_hash=pack_a.text_hash)
    packs = {
        "a": pack_a.level,
        "b": pack_b_level,
        "disagreement": pack_a.level != pack_b_level,
    }
    return combined, packs


def grade_action_with_packs(
    tool: str, args: dict[str, object], history_categories: list[set[str]] | None = None
) -> tuple[Grade, dict[str, object]]:
    """Grade an action with both packs; returns (combined Grade, packs dict)."""
    pack_a = grade_action(tool, args, history_categories)
    text = _text_of(args)[:MAX_GRADE_CHARS]
    pack_b_level, pack_b_categories, pack_b_reasons = _grade_pack_b(tool, text)
    return _with_packs(pack_a, pack_b_level, pack_b_categories, pack_b_reasons)


def grade_output_with_packs(tool: str, output: object) -> tuple[Grade, dict[str, object]]:
    """Grade an output with both packs; returns (combined Grade, packs dict)."""
    pack_a = grade_output(tool, output)
    text = _text_of(output)[:MAX_GRADE_CHARS]
    pack_b_level, pack_b_categories, pack_b_reasons = _grade_pack_b(tool, text)
    return _with_packs(pack_a, pack_b_level, pack_b_categories, pack_b_reasons)


def calibration_report(grades: list[dict[str, object]]) -> dict[str, object]:
    """Aggregate stored grade dicts (``{level, packs}``) into a report.

    The shadow-calibration artifact: how often the packs agree, where
    they disagree, and which levels each pack produces. Grades carry
    only hashes, never graded text, so the report is safe to persist.
    """
    by_level: dict[str, int] = {}
    by_pack_a: dict[str, int] = {}
    by_pack_b: dict[str, int] = {}
    disagreements = 0
    for grade in grades:
        level = str(grade.get("level") or "unknown")
        by_level[level] = by_level.get(level, 0) + 1
        packs = grade.get("packs") if isinstance(grade.get("packs"), dict) else {}
        if packs:
            by_pack_a[str(packs.get("a"))] = by_pack_a.get(str(packs.get("a")), 0) + 1
            by_pack_b[str(packs.get("b"))] = by_pack_b.get(str(packs.get("b")), 0) + 1
            if packs.get("disagreement"):
                disagreements += 1
    total = len(grades)
    return {
        "total": total,
        "by_level": by_level,
        "by_pack": {"a": by_pack_a, "b": by_pack_b},
        "disagreements": disagreements,
        "disagreement_rate": (disagreements / total) if total else 0.0,
    }


def grade_output(tool: str, output: object) -> Grade:
    """Grade a tool output (post-execution).

    Only secret + injection rules: output text does not act, so
    destructive/egress patterns quoted in output are not actions.
    They are caught when the model actually proposes them next round.
    """
    try:
        text = _text_of(output)[:MAX_GRADE_CHARS]
        text_hash = _hash(text)
        levels: list[str] = []
        categories: set[str] = set()
        reasons: list[str] = []
        secret = _secret_reasons(text)
        if secret:
            levels.append("high")
            categories.add("secret")
            reasons.extend(secret)
        injection_level, injection_reasons = _injection_grade(text)
        if injection_level != "low":
            levels.append(injection_level)
            categories.add("injection")
            reasons.extend(injection_reasons)
        return _aggregate(levels, categories, reasons, text_hash)
    except Exception as exc:  # noqa: BLE001 — fail closed, never silent LOW
        logger.warning("content grade failed, UNKNOWN (fail-closed) | tool=%s err=%s", tool, exc)
        return Grade(level="unknown", categories=frozenset({"unknown"}), reasons=(f"grader failed: {exc}",))


# --- mode + disposition ----------------------------------------------------- #

_MODE_OVERRIDE: str | None = None


def get_grade_mode() -> str:
    """Current mode: ``shadow`` (default) or ``enforce``.

    An explicit but unknown value fails closed to ``enforce`` (a typo
    must not silently shadow); unset means ``shadow``.
    """
    if _MODE_OVERRIDE is not None:
        return _MODE_OVERRIDE
    mode = os.environ.get("CONTENT_GRADE_MODE", "shadow").strip().lower()
    if mode not in ("shadow", "enforce"):
        logger.warning("unknown CONTENT_GRADE_MODE %r, failing closed to enforce", mode)
        return "enforce"
    return mode


def set_grade_mode(mode: str) -> None:
    """Override the mode (tests)."""
    global _MODE_OVERRIDE
    _MODE_OVERRIDE = mode


def reset_grade_mode() -> None:
    """Clear the override (test teardown)."""
    global _MODE_OVERRIDE
    _MODE_OVERRIDE = None


def disposition(grade: Grade, mode: str | None = None) -> str:
    """Map a grade to an action: allow | warn | refuse.

    Shadow always allows (record only) — that is the point of the
    shadow period. Enforce refuses HIGH and UNKNOWN (fail-closed),
    warns on MEDIUM, allows LOW. Suspension-for-confirm (the
    ConfirmRisky mapping) is a follow-up: it reuses the Phase 4
    PendingAction staging once calibration data exists.
    """
    mode = mode or get_grade_mode()
    if mode == "shadow":
        return "allow"
    if grade.level == "high" or grade.level == "unknown":
        return "refuse"
    if grade.level == "medium":
        return "warn"
    return "allow"
