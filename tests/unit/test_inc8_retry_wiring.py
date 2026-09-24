"""INC8-A6-③ — ``resilience.retry`` is now wired to the external-fault call
sites: a *transient* failure is retried instead of immediately degrading.

Targets are explicitly the external collaborators — the MCP tool fetch and the
A2A HTTP transport — never the LLM inference path (retrying that would only add
latency for no benefit). The retry policy is tenacity's ``_RETRYABLE`` set:
timeouts / network errors / connection resets are retried; anything else
(e.g. a ``ValueError`` or an HTTP status error) is not.
"""

from __future__ import annotations

import sys
import types

import httpx
import pytest

from forgeflow.a2a.protocol import A2AMessage
from forgeflow.a2a.transport import HTTPTransport
from forgeflow.mcp.client import adapter


def _install_fake_mcp_client(monkeypatch, get_tools) -> None:
    """Install a fake ``langchain_mcp_adapters`` exposing ``MultiServerMCPClient``."""

    _inner = get_tools  # closure cell: readable from the method body

    class _FakeClient:
        def __init__(self, *_a, **_k):
            pass

        async def get_tools(self):
            return await _inner(self)

    fake_client_mod = types.ModuleType("langchain_mcp_adapters.client")
    fake_client_mod.MultiServerMCPClient = _FakeClient
    fake_pkg = types.ModuleType("langchain_mcp_adapters")
    fake_pkg.client = fake_client_mod
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters", fake_pkg)
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters.client", fake_client_mod)


@pytest.mark.asyncio
async def test_mcp_tool_fetch_retries_a_transient_error(monkeypatch):
    calls = {"n": 0}

    async def _get_tools(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("transient flap")
        return []

    _install_fake_mcp_client(monkeypatch, _get_tools)

    tools = await adapter.get_mcp_tools()

    assert tools == []
    assert calls["n"] == 2  # the transient failure was retried once, then succeeded


@pytest.mark.asyncio
async def test_mcp_tool_fetch_does_not_retry_a_fatal_error(monkeypatch):
    calls = {"n": 0}

    async def _get_tools(self):
        calls["n"] += 1
        raise ValueError("not retryable")

    _install_fake_mcp_client(monkeypatch, _get_tools)

    tools = await adapter.get_mcp_tools()

    assert tools == []
    assert calls["n"] == 1  # a non-transient error is not retried


@pytest.mark.asyncio
async def test_a2a_http_transport_retries_a_transient_error(monkeypatch):
    calls = {"n": 0}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"method": "tasks/send", "params": {}}

    class _FakeAsyncClient:
        def __init__(self, *_a, **_k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def post(self, *_a, **_k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("transient flap")
            return _FakeResponse()

    monkeypatch.setattr("forgeflow.a2a.transport.httpx.AsyncClient", _FakeAsyncClient)

    result = await HTTPTransport().send(A2AMessage(method="tasks/send"), "http://agent")

    assert result is not None
    assert result.method == "tasks/send"
    assert calls["n"] == 2  # retried the transient failure, then succeeded
