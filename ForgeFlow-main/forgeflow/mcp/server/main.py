"""ForgeFlow MCP Tool Server — FastMCP with HTTP and stdio transport modes.

Usage:
  HTTP (production):  python -m forgeflow.mcp.server.main http
  stdio (local CLI):  python -m forgeflow.mcp.server.main stdio

The HTTP transport exposes tools at POST /mcp (streamable-HTTP per MCP spec).
Agents connect via the client adapter in forgeflow/mcp/client/adapter.py.
"""

from __future__ import annotations

import logging
import sys

from fastmcp import FastMCP

from forgeflow.config import get_settings
from forgeflow.mcp.server.tools import (
    crm_tools,
    data_tools,
    email_tools,
    github_tools,
    hubspot_tools,
    jira_tools,
    msgraph_tools,
    multimodal_tools,
    platform_tools,
    quickbooks_tools,
    salesforce_tools,
    sap_tools,
    search_tools,
    servicenow_tools,
    slack_tools,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Main MCP server — compose all tool sub-routers
mcp = FastMCP(
    name="ForgeFlow Tool Server",
    version="1.0.0",
    instructions=(
        "This server exposes enterprise tools for ForgeFlow agents: "
        "web search, CRM management, email sending, and data enrichment."
    ),
)

mcp.mount(search_tools.router, namespace="search")
mcp.mount(crm_tools.router, namespace="crm")
mcp.mount(email_tools.router, namespace="email")
mcp.mount(data_tools.router, namespace="data")
mcp.mount(slack_tools.router, namespace="slack")
mcp.mount(github_tools.router, namespace="github")
mcp.mount(jira_tools.router, namespace="jira")
mcp.mount(hubspot_tools.router, namespace="hubspot")
mcp.mount(salesforce_tools.router, namespace="salesforce")
mcp.mount(servicenow_tools.router, namespace="servicenow")
mcp.mount(sap_tools.router, namespace="sap")
mcp.mount(quickbooks_tools.router, namespace="quickbooks")
mcp.mount(msgraph_tools.router, namespace="msgraph")
mcp.mount(multimodal_tools.router, namespace="multimodal")
mcp.mount(platform_tools.router, namespace="platform")


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):  # noqa: ANN001
    """Liveness probe for the HTTP transport. The MCP protocol lives at POST
    /mcp (which 406s a plain GET), so container healthchecks hit this instead."""
    from starlette.responses import JSONResponse

    return JSONResponse({"status": "ok", "service": "forgeflow-mcp"})


if __name__ == "__main__":
    settings = get_settings()
    mode = sys.argv[1] if len(sys.argv) > 1 else "http"

    if mode == "stdio":
        logger.info("Starting MCP server in stdio mode")
        mcp.run(transport="stdio")
    else:
        logger.info(
            "Starting MCP server in HTTP mode on %s:%d",
            settings.mcp_server_host,
            settings.mcp_server_port,
        )
        mcp.run(
            transport="streamable-http",
            host=settings.mcp_server_host,
            port=settings.mcp_server_port,
        )
