"""MCP client adapter — converts MCP server tools to LangChain BaseTool instances.

The adapter connects to the MCP HTTP server and returns a list of LangChain-
compatible tools that can be passed to BaseAgent (and bound to the configured
chat model).

**Every tool is wrapped by the tool-output guard before it is returned**
(:func:`forgeflow.security.tool_output_guard.guard_tool_output`). MCP tool results
are external, untrusted content — a scraped page or a CRM note can carry
instructions aimed at the model. This adapter is the boundary between the tool
layer and the agent, which is exactly where that filter belongs (SECURITY_AUDIT
C-5 / architecture review #6). Without the wrap the guard would be a
well-tested function nobody calls.

Falls back to an empty list if the MCP server is unavailable, so agents still
run in degraded mode (useful for testing without the server running).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from langchain_core.tools import StructuredTool

from forgeflow.config import get_settings
from forgeflow.mcp.auth import active_run, approved_grant, identity, issue_token
from forgeflow.resilience.retry import retry_async
from forgeflow.security.tool_output_guard import guard_tools

logger = logging.getLogger(__name__)
_status = {"status": "not_checked", "tool_count": 0, "updated_at": None, "error": None}
_definitions: dict = {}


def connection_status() -> dict:
    return dict(_status)


def connection_failed(error: Exception) -> None:
    from forgeflow.mcp.client.registry import clear

    clear()
    _status.update(
        status="unavailable",
        tool_count=0,
        updated_at=datetime.now(UTC).isoformat(),
        error=type(error).__name__,
    )


def result_data(result: Any) -> dict:
    """Preserve machine data; the text projection is separately guarded."""
    if result.is_error:
        raise RuntimeError("MCP server returned a tool error")
    data = result.structured_content
    if data is None:
        text = "\n".join(getattr(part, "text", "") for part in result.content)
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            data = {"text": text}
    if isinstance(data, dict) and (data.get("ok") is False or data.get("error")):
        raise RuntimeError("MCP business operation failed")
    return data if isinstance(data, dict) else {"value": data}


async def _load_tools(target: Any) -> list:
    """Use the maintained FastMCP client; SDK-major details stay at this seam."""
    auth = issue_token("", "discovery", "discovery", []) if isinstance(target, str) else None
    from forgeflow.mcp.client.sessions import make_client

    async with make_client(target, auth) as client:
        definitions = await client.list_tools()
    from forgeflow.mcp.client.registry import clear

    clear()
    tools = []
    for definition in definitions:

        def build(name: str):
            async def invoke(**arguments):
                current = identity.get()
                grant = approved_grant(name, current[0], current[2]) if current else None
                if current is None or grant is None or grant["read_only"] is not True:
                    raise PermissionError("No approved MCP identity or tool grant")
                from forgeflow.mcp.client.registry import tool_id
                from forgeflow.runtime.gate import check_tool_permission
                from forgeflow.runtime.tool_executor import ToolCallContext, ToolExecutor

                if not check_tool_permission(current[2], tool_id(name)):
                    raise PermissionError("MCP runtime permission denied")
                invocation = await ToolExecutor().execute(
                    tool_id(name),
                    ctx=ToolCallContext(
                        run_id=active_run.get(),
                        step_id="mcp:" + name,
                        tenant_id=current[0],
                        user_id=current[1],
                        role=current[2],
                        args=arguments,
                    ),
                )
                if invocation.status != "ok":
                    raise RuntimeError(invocation.error or "MCP tool execution failed")
                data = invocation.payload["data"]
                return json.dumps(data, ensure_ascii=False), data

            return invoke

        tools.append(
            StructuredTool.from_function(
                coroutine=build(definition.name),
                name=definition.name,
                description=definition.description or definition.name,
                args_schema=definition.input_schema,
                response_format="content_and_artifact",
            )
        )
        _definitions[definition.name] = definition
        from forgeflow.mcp.client.registry import install

        install(definition, target)
    return tools


async def get_mcp_tools() -> list:
    """Connect to the MCP server and return LangChain-compatible, guarded tools.

    Returns empty list on connection failure (graceful degradation).
    """
    settings = get_settings()
    host = settings.mcp_server_host
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    mcp_url = f"http://{host}:{settings.mcp_server_port}/mcp"

    try:
        # The MCP server is an external collaborator: a *transient* transport
        # failure (a flaky connection) should be retried rather than degrading to
        # "no tools" on the first hiccup. This is the consumer that makes
        # ``resilience.retry`` real, and it deliberately targets the MCP / HTTP
        # path only — never the LLM inference path (retrying that would inflate
        # latency for no benefit).
        tools = await retry_async(
            lambda: _load_tools(mcp_url), max_attempts=3, min_wait=0.2, max_wait=1.0
        )
        # Sanitise on the way out: the agent must never see a raw MCP payload.
        guarded = guard_tools(list(tools))
        _status.update(
            status="connected",
            tool_count=len(guarded),
            updated_at=datetime.now(UTC).isoformat(),
            error=None,
        )
        logger.info("Loaded %d guarded tools from MCP server at %s", len(guarded), mcp_url)
        return guarded

    except Exception as e:
        connection_failed(e)
        logger.warning(
            "MCP server at %s unavailable (%s) — running without external tools",
            mcp_url,
            e,
        )
        return []


async def maintain_discovery():
    """Refresh read-only discovery; tool calls are never replayed here."""
    import asyncio

    while True:
        await asyncio.sleep(60)
        await get_mcp_tools()
