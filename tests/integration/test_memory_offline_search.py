"""P1-2 integration: ``GET /memory/search`` must work in the offline profile.

The route path is the exact one QA reproduced (500 ``openai.OpenAIError: Missing
credentials``). We provide a real async-context-manager pool returning no rows —
the embedding step now uses the deterministic offline embedder instead of
reaching for an OpenAI key.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient


def _offline_pool():
    """A pool whose acquire() is a proper async context manager → empty results."""
    pool = MagicMock()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value="OK")

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def test_memory_search_offline_returns_200():
    with patch("forgeflow.api.main.init_pool", new_callable=AsyncMock) as mock_pool, \
         patch("forgeflow.api.main.compile_graph", new_callable=AsyncMock), \
         patch("forgeflow.api.main.get_mcp_tools", new_callable=AsyncMock, return_value=[]):

        mock_pool.return_value = _offline_pool()

        from forgeflow.api.main import app
        from forgeflow.auth.jwt import create_access_token

        token = create_access_token(user_id="admin", role="admin")
        headers = {"Authorization": f"Bearer {token}"}

        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.get("/memory/search", params={"q": "hello"}, headers=headers)
            assert r.status_code == 200, r.text
            assert r.json() == []
