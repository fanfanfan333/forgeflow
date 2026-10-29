import asyncio
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from forgeflow.config import get_settings
from forgeflow.mcp.client import sessions


@pytest.mark.asyncio
async def test_clients_are_reused_but_identity_scopes_are_separate(monkeypatch):
    monkeypatch.setattr(
        get_settings(), "mcp_service_secret", SecretStr("separate-mcp-key-" + "x" * 32)
    )
    opens, tokens = [], []

    class Client:
        def __init__(self, target, **kwargs):
            opens.append(target)
            tokens.append(kwargs.get("auth"))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def call_tool(self, name, args, **kwargs):
            return SimpleNamespace(is_error=False, structured_content=args)

    monkeypatch.setattr(sessions, "Client", Client)
    try:
        for actor in ["one", "one", "two"]:
            await sessions.call_tool(
                "http://internal/mcp", "a", actor, "manager", "read", {"value": actor}
            )
        assert len(opens) == 2 and len(set(tokens)) == 2
    finally:
        await sessions.close_sessions()


@pytest.mark.asyncio
async def test_timeout_after_dispatch_is_unknown_and_not_replayed(monkeypatch):
    monkeypatch.setattr(
        get_settings(), "mcp_service_secret", SecretStr("separate-mcp-key-" + "x" * 32)
    )
    dispatched = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def call_tool(self, *args, **kwargs):
            dispatched.append(True)
            raise TimeoutError("server may have committed")

    monkeypatch.setattr(sessions, "Client", Client)
    try:
        with pytest.raises(sessions.OutcomeUnknown):
            await sessions.call_tool("http://internal/mcp", "a", "one", "manager", "write", {})
        assert len(dispatched) == 1
    finally:
        await sessions.close_sessions()


@pytest.mark.asyncio
async def test_parent_cancellation_reaches_the_inflight_mcp_call(monkeypatch):
    monkeypatch.setattr(
        get_settings(), "mcp_service_secret", SecretStr("separate-mcp-key-" + "x" * 32)
    )
    started, stopped = asyncio.Event(), asyncio.Event()

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def call_tool(self, *args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    monkeypatch.setattr(sessions, "Client", Client)
    task = asyncio.create_task(
        sessions.call_tool("http://internal/mcp", "a", "one", "manager", "read", {})
    )
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(stopped.wait(), 2)
    finally:
        await sessions.close_sessions()
