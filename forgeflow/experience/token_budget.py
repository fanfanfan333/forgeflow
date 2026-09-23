"""Token-budget estimation + trimming helpers (INC2 A5, architecture §2.5).

Deliberately dependency-free: we estimate tokens with a cheap character-based
heuristic instead of pulling in ``tiktoken`` (which is offline-hostile and adds a
hard dependency). The heuristic is good enough for budgeting context:

  * CJK ideographs ≈ 1.7 chars/token
  * Everything else ≈ 4 chars/token

``estimate_tokens`` blends the two so mixed Chinese/English text is handled
sensibly, and never returns 0 for non-empty text.
"""

from __future__ import annotations

import math

__all__ = [
    "CJK_CHARS_PER_TOKEN",
    "DEFAULT_CHARS_PER_TOKEN",
    "estimate_tokens",
    "per_item_cap",
    "truncate_to_tokens",
    "fits_budget",
]

# Chinese renders roughly 1.7 characters per token; latin/ASCII ≈ 4.
CJK_CHARS_PER_TOKEN = 1.7
DEFAULT_CHARS_PER_TOKEN = 4.0


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF  # CJK Unified Ideographs
        or 0x3400 <= code <= 0x4DBF  # Extension A
        or 0xF900 <= code <= 0xFAFF  # Compatibility Ideographs
        or 0x3040 <= code <= 0x30FF  # Hiragana / Katakana
        or 0xAC00 <= code <= 0xD7AF  # Hangul syllables
    )


def estimate_tokens(text: str | None) -> int:
    """Estimate the token count of ``text`` (ceiling, always >= 0).

    Blends the CJK ratio (``len/1.7``) with the ASCII ratio (``len/4``): a
    string of ``c`` CJK chars and ``n`` other chars costs
    ``ceil(c / 1.7 + n / 4)`` tokens.
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if _is_cjk(ch))
    other = len(text) - cjk
    tokens = cjk / CJK_CHARS_PER_TOKEN + other / DEFAULT_CHARS_PER_TOKEN
    return max(1, math.ceil(tokens))


def per_item_cap(budget_tokens: int, ratio: float = 0.4) -> int:
    """Per-item token ceiling: ``budget × ratio`` (>= 1)."""
    return max(1, int(budget_tokens * ratio))


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Hard-truncate ``text`` so ``estimate_tokens(result) <= max_tokens``.

    Truncation is char-based (cheap) and appends an ellipsis marker so a
    downstream reader can tell the text was clipped. Falls back to the raw text
    when it already fits.
    """
    if max_tokens <= 0:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text

    # Convert the token budget back to an approximate character budget using the
    # text's own CJK ratio, then trim with a small safety margin.
    cjk = sum(1 for ch in text if _is_cjk(ch))
    ratio_cjk = cjk / len(text) if text else 0.0
    chars_per_token = (
        ratio_cjk * CJK_CHARS_PER_TOKEN + (1 - ratio_cjk) * DEFAULT_CHARS_PER_TOKEN
    )
    approx_chars = max(1, int(max_tokens * chars_per_token))

    def _fits(candidate: str) -> bool:
        # The ellipsis marker counts against the budget too.
        return estimate_tokens(candidate + "…") <= max_tokens

    clipped = text[:approx_chars]
    # Walk back until the (ellipsis-inclusive) result fits — guards against the
    # ratio estimate overshooting.
    while clipped and not _fits(clipped):
        clipped = clipped[: max(0, len(clipped) - max(1, len(clipped) // 10))]
    return clipped + "…"


def fits_budget(text: str, budget_tokens: int) -> bool:
    """True when ``text`` fits within ``budget_tokens``."""
    return estimate_tokens(text) <= budget_tokens
