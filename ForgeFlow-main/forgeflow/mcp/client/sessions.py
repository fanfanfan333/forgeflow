"""Bounded identity-isolated clients owned and closed by their worker task."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from fastmcp import Client

from forgeflow.config import get_settings
from forgeflow.mcp.auth import issue_token


def make_client(target: Any, token: str | None):
    options = {"timeout": get_settings().mcp_call_timeout_seconds}
    if isinstance(target, str) and urlsplit(target).hostname in {"localhost", "127.0.0.1", "::1"}:
        import httpx2
        from fastmcp.client.transports import StreamableHttpTransport

        def factory(
            headers: dict[str, str] | None = None,
            timeout: httpx2.Timeout | None = None,
            auth: httpx2.Auth | None = None,
            **kwargs: Any,
        ) -> httpx2.AsyncClient:
            return httpx2.AsyncClient(
                **{
                    **kwargs,
                    "headers": headers,
                    "timeout": timeout,
                    "auth": auth,
                    "trust_env": False,
                    "follow_redirects": False,
                }
            )

        return Client(
            StreamableHttpTransport(target, auth=token, httpx_client_factory=factory), **options
        )
    return Client(target, auth=token if isinstance(target, str) else None, **options)


class OutcomeUnknown(RuntimeError):
    """Dispatch may have happened; do not replay a side effect."""


@dataclass
class Session:
    key: tuple
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=16))
    created: float = field(default_factory=time.monotonic)
    worker: asyncio.Task | None = None

    async def run(self):
        target, tenant, actor, role, tools = self.key
        token = issue_token(tenant, actor, role, list(tools))
        active = None
        try:
            async with make_client(target, token) as client:
                while True:
                    item = await asyncio.wait_for(self.queue.get(), timeout=90)
                    if item is None:
                        break
                    name, args, active = item
                    if active.cancelled():
                        active = None
                        continue
                    try:
                        operation = asyncio.create_task(
                            client.call_tool(name, args, raise_on_error=False)
                        )
                        active.add_done_callback(
                            lambda future, call=operation: (
                                call.cancel() if future.cancelled() else None
                            )
                        )
                        result = await operation
                    except Exception as exc:
                        if not active.done():
                            active.set_exception(
                                OutcomeUnknown(
                                    "MCP call outcome unknown; inspect external state before retry"
                                )
                            )
                        raise exc
                    if not active.done():
                        active.set_result(result)
                    active = None
        except (Exception, asyncio.CancelledError) as exc:
            if isinstance(target, str) and not isinstance(exc, asyncio.CancelledError):
                from forgeflow.mcp.client.adapter import connection_failed

                connection_failed(exc)
            if active is not None and not active.done():
                active.set_exception(OutcomeUnknown("MCP connection closed during dispatch"))
        finally:
            while not self.queue.empty():
                item = self.queue.get_nowait()
                if item and not item[2].done():
                    item[2].set_exception(ConnectionError("MCP operation not started"))


_sessions: dict[tuple, Session] = {}


async def call_tool(target: Any, tenant: str, actor: str, role: str, name: str, arguments: dict):
    key = (target, tenant, actor, role, (name,))
    for old_key, old in list(_sessions.items()):
        assert old.worker is not None
        if old.worker.done():
            del _sessions[old_key]
    session = _sessions.get(key)
    if session is not None and time.monotonic() - session.created >= 90:
        if not session.queue.empty():
            raise RuntimeError("MCP session renewal is busy")
        await session.queue.put(None)
        assert session.worker is not None
        await session.worker
        del _sessions[key]
        session = None
    if session is None:
        if len(_sessions) >= 32:
            raise RuntimeError("MCP session capacity reached")
        session = Session(key)
        session.worker = asyncio.create_task(session.run())
        _sessions[key] = session
    future = asyncio.get_running_loop().create_future()
    session.queue.put_nowait((name, arguments, future))
    return await future


async def close_sessions():
    sessions = list(_sessions.values())
    _sessions.clear()
    for session in sessions:
        session.worker.cancel()
    if sessions:
        await asyncio.wait_for(
            asyncio.gather(*(s.worker for s in sessions), return_exceptions=True), 5
        )
