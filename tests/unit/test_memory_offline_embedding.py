"""P1-2 regression: the legacy pgvector memory path must work offline.

``/memory/search`` → ``MemoryManager`` → ``PGVectorStore`` used to build an
external embedder unconditionally, so with no embedding key every search
returned 500 ``OpenAIError: Missing credentials``. INC16: embeddings are now
**always** the dependency-free deterministic local implementation, so the
pgvector path is offline by construction.
"""

from __future__ import annotations

import pytest

from forgeflow.config import get_settings
from forgeflow.memory.pgvector_store import PGVectorStore, _OfflineEmbeddings

pytestmark = pytest.mark.asyncio


async def test_store_uses_offline_embeddings():
    store = PGVectorStore(pool=None)  # type: ignore[arg-type]
    assert isinstance(store._get_embeddings(), _OfflineEmbeddings)


async def test_offline_embedding_is_deterministic_and_dimensioned():
    dim = get_settings().embedding_dimension
    emb = _OfflineEmbeddings(dim)
    v1 = await emb.aembed_query("hello world")
    v2 = await emb.aembed_query("hello world")
    assert v1 == v2
    assert len(v1) == dim
    assert any(x != 0.0 for x in v1)


async def test_memory_manager_recall_offline(mock_pool):
    """The QA reproduction: recall() must not raise without an OpenAI key."""
    from forgeflow.memory.memory_manager import MemoryManager

    results = await MemoryManager(mock_pool).recall(query="hello", k=3, namespace="global")
    assert results == []
