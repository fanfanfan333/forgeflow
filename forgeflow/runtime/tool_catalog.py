"""Per-run schema discovery; never grants execution permissions."""

from __future__ import annotations

import json
import re
from typing import Any

from forgeflow.config import get_settings
from forgeflow.experience.token_budget import estimate_tokens

SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "tools.search",
        "description": "Search approved tools by purpose or name and load their exact parameter schemas before calling them.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 200},
                "limit": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}


def deferred(schemas: list[dict]) -> bool:
    mode = get_settings().agent_tool_loading
    return mode == "deferred" or (
        mode == "auto"
        and (
            estimate_tokens(json.dumps(schemas, ensure_ascii=False))
            > get_settings().ollama_num_ctx // 2
            or sum(item["function"]["name"].startswith("mcp.") for item in schemas) >= 8
        )
    )


def visible(schemas: list[dict], loaded: set[str]) -> list[dict]:
    return [SEARCH_SCHEMA] + [item for item in schemas if item["function"]["name"] in loaded]


def search(schemas: list[dict], query: Any, limit: int = 3) -> list[dict]:
    if (
        not isinstance(query, str)
        or not query.strip()
        or len(query) > 200
        or type(limit) is not int
        or not 1 <= limit <= 5
    ):
        raise ValueError("Tool search requires a query and limit between 1 and 5")
    terms = set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", query.lower()))
    ranked = sorted(
        schemas,
        key=lambda item: (
            -sum(
                term in json.dumps(item["function"], ensure_ascii=False).lower() for term in terms
            ),
            item["function"]["name"],
        ),
    )
    return [
        item
        for item in ranked[:limit]
        if any(term in json.dumps(item["function"], ensure_ascii=False).lower() for term in terms)
    ]


async def search_handler(args: dict, ctx: Any) -> dict:
    task = ctx.task_context
    if task is None:
        raise PermissionError("Tool search requires a scoped run")
    schemas = task.context.get("_approved_tool_schemas", [])
    found = search(schemas, args.get("query"), args.get("limit", 3))
    loaded = task.context.setdefault("_loaded_tools", [])
    for schema in found:
        name = schema["function"]["name"]
        if name not in loaded:
            loaded.append(name)
    return {"ok": True, "tools": found, "summary": f"Loaded {len(found)} approved tool schemas"}
