"""Rebuild the compiled business graphs when MCP discovery really changed.

A LangGraph business graph binds its tool list at **compile** time. MCP
discovery, however, keeps running in the background: a server that was down at
boot and came back later produced a state where ``/mcp/status`` reported
"connected" while every compiled agent still held the empty startup list — the
new tools existed in the registry but nothing that could call them.

This module owns the one decision that fixes it, in a form that can be tested
without standing up the whole application lifespan:

* compare discovered tool names, descriptions and parameter schemas;
* recompile **only** when that set differs from what the graphs were built
  with, so an unchanged 60-second poll avoids rebuilding;
* swap ``state.mcp_tools`` / ``state.graphs`` / ``state.graph`` atomically only
  after every graph compiled successfully.

``state`` is anything with those three attributes (``app.state`` in production,
a stub in tests), and ``compile_graph`` is injected so the tests exercise the
decision logic rather than the model wiring.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

#: The workflow types the application compiles one graph per. Kept here (not in
#: ``main.py``) so the refresh path and the startup path cannot drift apart.
WORKFLOW_TYPES: tuple[str, ...] = ("sales_ops", "support_ops", "finance_recon")

#: The workflow type exposed as ``app.state.graph`` for legacy code paths.
DEFAULT_WORKFLOW_TYPE = "sales_ops"


def tool_names(tools: list[Any] | None) -> set[str]:
    """The discovered tool names, ignoring unnamed entries."""
    return {str(getattr(tool, "name", "") or "") for tool in (tools or [])} - {""}


async def rebuild_graphs_if_changed(
    state: Any,
    tools: list[Any] | None,
    compile_graph: Callable[..., Awaitable[Any]],
) -> bool:
    """Recompile the business graphs iff the tool set changed.

    Returns ``True`` when a rebuild happened. Compilation failures propagate:
    the caller logs them and keeps serving the previous graphs, which is the
    only honest option (half-swapped graphs would be worse than stale ones).
    """
    def definitions(items: list[Any] | None) -> list[str]:
        result = []
        for tool in items or []:
            if not getattr(tool, "name", ""):
                continue
            schema = getattr(tool, "args_schema", None)
            if hasattr(schema, "model_json_schema"):
                schema = schema.model_json_schema()
            result.append(json.dumps({"name": tool.name, "description": getattr(tool, "description", ""),
                                      "parameters": schema}, sort_keys=True, ensure_ascii=False))
        return sorted(result)

    current = definitions(getattr(state, "mcp_tools", None))
    incoming = definitions(tools)
    if incoming == current:
        return False

    fresh = {
        workflow_type: await compile_graph(
            mcp_tools=tools, workflow_type=workflow_type
        )
        for workflow_type in WORKFLOW_TYPES
    }

    state.mcp_tools = list(tools or [])
    state.graphs = fresh
    state.graph = fresh[DEFAULT_WORKFLOW_TYPE]
    logger.info(
        "MCP tool set changed (%d → %d); business graphs recompiled",
        len(current),
        len(incoming),
    )
    return True
