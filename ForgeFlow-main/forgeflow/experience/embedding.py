"""Dependency-free deterministic embeddings for the offline profile.

``deterministic_embedding`` uses signed feature hashing (a bag-of-tokens
sketch): identical text yields cosine 1.0 and texts that share vocabulary land
close together, so the Skill Candidate Compiler's similarity gate behaves
sensibly with no OpenAI key. When a real key is present and
``EMBEDDING_PROVIDER=openai`` we defer to langchain's embedder.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any

from forgeflow.config import get_settings

_TOKEN_RE = re.compile(r"[a-z0-9\u4e00-\u9fff]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def deterministic_embedding(text: str, dimension: int = 1536) -> list[float]:
    """Signed feature-hashed bag-of-tokens vector, L2-normalised."""
    vector = [0.0] * dimension
    for token in _tokenize(text):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        index = value % dimension
        sign = 1.0 if (value >> 63) & 1 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(x * x for x in vector))
    if norm <= 0.0:
        # Empty/only-punctuation text — return a stable unit vector so the
        # vector column is never all-zeros (pgvector cosine distance is 1.0).
        vector[0] = 1.0
        return vector
    return [x / norm for x in vector]


def _try_openai_embedding(text: str) -> list[float] | None:
    """Best-effort real embedding. Returns ``None`` when unavailable."""
    try:
        settings = get_settings()
        key = settings.openai_api_key.get_secret_value()
        if not key:
            return None
        from langchain_openai import OpenAIEmbeddings  # lazy optional import

        embedder = OpenAIEmbeddings(api_key=key)
        return [float(x) for x in embedder.embed_query(text)]
    except Exception:  # noqa: BLE001 — any failure degrades to the offline path
        return None


def embed_text(text: str, dimension: int | None = None) -> list[float]:
    """Embed ``text`` using the configured provider, degrading to the offline
    deterministic implementation when OpenAI is not configured/reachable."""
    settings = get_settings()
    dim = dimension or settings.embedding_dimension
    if settings.embedding_provider.lower() == "openai":
        vector = _try_openai_embedding(text)
        if vector is not None:
            return vector
    return deterministic_embedding(text, dim)


def cosine_similarity(a: list[float] | None, b: list[float] | None) -> float:
    """Cosine similarity in [-1, 1]; 0.0 for missing vectors."""
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = na = nb = 0.0
    for i in range(n):
        x = float(a[i])
        y = float(b[i])
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def as_dict(value: Any) -> dict[str, Any]:
    """Small helper: coerce a dataclass/dict to a plain dict."""
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return dict(value)
