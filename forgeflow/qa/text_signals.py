"""Line-anchored text signals shared by the QA core (§40/§42).

A single home for the "did the transcript say nothing ran?" heuristic, so
:mod:`forgeflow.qa.executor` (which derives an evidence status from a real
process outcome) and :mod:`forgeflow.qa.analyzer` (which classifies a failure)
share **one** implementation instead of two independently drifting copies.

Why line-anchored
-----------------
A notice counts only when a *line* **is** the notice (optionally wrapped in
pytest's ``=`` banner) — never when the words merely appear inside a longer
line. Aggregate targets (``check`` / ``test`` / ``test-integration``) emit the
whole suite's verbose transcript, so a descriptive test id such as
``…status_matrix[ok-0-no tests ran in 0.01s-…]`` or an unrelated log line must
never be mistaken for "the run executed nothing". Matching at the *start* of the
(stripped) line is what keeps a passing parametrised id from deciding a failure's
attribution.

Dependencies: **standard library only** — besides ``re`` this module imports
nothing, not even from the rest of the sub-package, so it can never create an
import cycle.
"""

from __future__ import annotations

import re

#: pytest's "the run executed nothing" notices. Kept as a prefix match so both
#: the bare form and its timed variant (``no tests ran in 0.01s``) count.
NOTHING_RAN_PHRASES: tuple[str, ...] = ("no tests ran", "collected 0 items")

#: The collection-harness family — the notices above plus the explicit
#: collection-error phrasings the classifier treats as a test-harness problem.
COLLECTION_NOTICE_PHRASES: tuple[str, ...] = (
    "no tests ran",
    "collected 0 items",
    "errors during collection",
    "error during collection",
)


def line_is_notice(line: str, phrases: tuple[str, ...]) -> bool:
    """True iff ``line`` **is** one of the ``phrases`` notices.

    The line is stripped of surrounding whitespace and any pytest ``=`` banner
    before comparison, and a match requires the stripped line to *start with* a
    phrase. A phrase embedded inside a longer line (e.g. a parametrised test id)
    therefore never matches.

    Args:
        line: One physical line of a captured transcript.
        phrases: Lower-case notice prefixes to test (see
            :data:`NOTHING_RAN_PHRASES` / :data:`COLLECTION_NOTICE_PHRASES`).

    Returns:
        ``True`` when the line is one of the notices, otherwise ``False``.
    """
    core = line.strip().strip("=").strip()
    return any(core.startswith(phrase) for phrase in phrases)


def output_has_notice(output: str, phrases: tuple[str, ...]) -> bool:
    """True iff **any** line of ``output`` is one of the ``phrases`` notices.

    Args:
        output: A captured transcript (stdout, stderr, or both).
        phrases: Notice prefixes to look for on a line (see
            :func:`line_is_notice`).

    Returns:
        ``True`` when at least one line is a notice, otherwise ``False``.
    """
    return any(line_is_notice(line, phrases) for line in output.splitlines())


# ── §68 failure fingerprints: the shared, drift-proof signature normaliser ────
#: Token cap for a fingerprint (bounded, so a multi-megabyte transcript can never
#: inflate a signature). Generation (:mod:`forgeflow.qa.analyzer`) and clustering
#: (:mod:`forgeflow.qa.regression`) share this constant so the two can never
#: drift — the same "single fact source" discipline the notices already follow.
FINGERPRINT_MAX_TOKENS: int = 12

#: Volatile noise stripped before tokenising. None of these carry a root cause,
#: and every one of them changes between two runs of the *same* failure.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_ISO_TS_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)
_CLOCK_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
_WIN_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s\"']+")
#: An absolute POSIX path: two or more ``/segment`` parts whose first ``/`` is
#: not preceded by a word character (so a *relative* ``forgeflow/qa/x.py`` is
#: left intact — only genuinely absolute/rooted paths are treated as volatile).
_POSIX_PATH_RE = re.compile(r"(?<![\w.\-])(?:/[\w.\-]+){2,}")
_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
_TOKEN_RE = re.compile(r"[a-z][a-z0-9_]{2,}")

#: Pure-English glue words dropped from a signature (they add no signal).
_FINGERPRINT_STOPWORDS: frozenset[str] = frozenset(
    {"the", "and", "for", "with", "this", "that", "but", "are", "was", "were", "you", "your", "our"}
)


def normalise_signature(text: str, *, max_tokens: int = FINGERPRINT_MAX_TOKENS) -> list[str]:
    """Normalise a real transcript into a **stable** list of signature tokens.

    The rules are deterministic and offline: blank out ANSI escapes, timestamps,
    absolute paths and bare numbers (all volatile between runs), then lowercase,
    keep word tokens, drop pure glue words, de-duplicate, sort and truncate to
    ``max_tokens``. The same root cause therefore yields the same signature — and
    hence the same fingerprint — across runs.

    The volatile-noise passes run **before** lower-casing so the case-sensitive
    markers in an ISO-8601 timestamp (``T`` / ``Z``) are still matched.

    Args:
        text: A captured transcript (stdout, stderr, or both). May be empty.
        max_tokens: Upper bound on the returned token count (``<= 0`` ⇒ ``[]``).

    Returns:
        A sorted, de-duplicated, bounded list of lower-case signature tokens.
        An empty transcript yields ``[]`` (an honest "no signature recovered").
    """
    if max_tokens <= 0:
        return []
    cleaned = _ANSI_RE.sub(" ", text or "")
    cleaned = _ISO_TS_RE.sub(" ", cleaned)
    cleaned = _CLOCK_RE.sub(" ", cleaned)
    cleaned = _WIN_PATH_RE.sub(" ", cleaned)
    cleaned = _POSIX_PATH_RE.sub(" ", cleaned)
    cleaned = _NUMBER_RE.sub(" ", cleaned).lower()
    tokens = {tok for tok in _TOKEN_RE.findall(cleaned) if tok not in _FINGERPRINT_STOPWORDS}
    return sorted(tokens)[:max_tokens]


__all__ = [
    "COLLECTION_NOTICE_PHRASES",
    "FINGERPRINT_MAX_TOKENS",
    "NOTHING_RAN_PHRASES",
    "line_is_notice",
    "normalise_signature",
    "output_has_notice",
]
