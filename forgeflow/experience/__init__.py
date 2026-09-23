"""Memory Hub — Experience as a first-class asset + five-layer memory scopes."""

from __future__ import annotations

from forgeflow.experience.embedding import (
    cosine_similarity,
    deterministic_embedding,
    embed_text,
)
from forgeflow.experience.extractor import ExperienceExtractor, extract_experience
from forgeflow.experience.models import OUTCOMES, ExperienceRecord
from forgeflow.experience.scopes import (
    SCOPE_RULES,
    MemoryScope,
    is_valid_scope,
    list_scopes,
    scope_namespace,
)

__all__ = [
    "ExperienceRecord",
    "OUTCOMES",
    "ExperienceExtractor",
    "extract_experience",
    "MemoryScope",
    "SCOPE_RULES",
    "scope_namespace",
    "is_valid_scope",
    "list_scopes",
    "embed_text",
    "deterministic_embedding",
    "cosine_similarity",
]
