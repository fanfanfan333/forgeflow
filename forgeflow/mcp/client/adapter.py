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

from fastmcp import Client
from langchain_core.tools import StructuredTool

from forgeflow.config import get_settings
from forgeflow.resilience.retry import retry_async
from forgeflow.security.tool_output_guard import guard_tools

logger = logging.getLogger(__name__)


async def _load_tools(target) -> list:
    """Use the maintained FastMCP client; SDK-major details stay at this seam."""
    async with Client(target) as client:
        definitions = await client.list_tools()
    tools = []
    for definition in definitions:
        def build(name: str):
            async def invoke(**arguments):
                async with Client(target) as client:
                    result = await client.call_tool(name, arguments)
                    return json.dumps({
                        "content": [part.model_dump(mode="json") for part in result.content],
                        "structured_content": result.structured_content,
                    }, ensure_ascii=False)
            return invoke
        tools.append(StructuredTool.from_function(
            coroutine=build(definition.name), name=definition.name,
            description=definition.description or definition.name,
            args_schema=definition.input_schema,
        ))
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
        logger.info("Loaded %d guarded tools from MCP server at %s", len(guarded), mcp_url)
        return guarded

    except Exception as e:
        logger.warning(
            "MCP server at %s unavailable (%s) — running without external tools",
            mcp_url,
            e,
        )
        return []
