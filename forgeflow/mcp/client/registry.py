"""Bridge approved MCP definitions into the existing runtime registry."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from forgeflow.config import get_settings
from forgeflow.mcp.auth import approved_grant

_schemas: dict[str, dict] = {}


def clear():
    from forgeflow.runtime.tool_registry import unregister

    for identifier in _schemas:
        unregister(identifier)
    _schemas.clear()


def tool_id(name: str) -> str:
    return "mcp.internal." + name


def input_schema_of(definition: Any) -> dict[str, Any]:
    """Return an MCP tool definition's JSON input schema, FastMCP-major-proof.

    FastMCP 2.x exposed the schema as ``Tool.input_schema``; FastMCP 3.x
    exposes the same JSON schema as ``Tool.parameters`` instead, and
    ``inputSchema`` is only a serialization alias — accessing it as an
    attribute raises AttributeError (which is exactly how the two MCP
    tests started failing on a fresh environment with fastmcp 3.4.11,
    while the older Windows environment with fastmcp 2.x still passed).
    A pydantic-shaped schema is dumped to a plain dict; anything else
    degrades to {} so callers fail closed on the schema, not on import.
    """
    for attr in ("input_schema", "parameters", "inputSchema"):
        try:
            value = getattr(definition, attr)
        except Exception:  # noqa: BLE001 — pydantic raises on unknown attrs
            continue
        if isinstance(value, dict):
            return value
        if value is not None and hasattr(value, "model_dump"):
            try:
                dumped = value.model_dump()
            except Exception:  # noqa: BLE001 — fall through to the next attr
                continue
            if isinstance(dumped, dict):
                return dumped
    return {}


def _grantable(grant: Any) -> bool:
    """Whether a deployment grant may reach the runtime registry at all.

    Read grants always may. A write grant may only when it declares the per-call
    approval policy — the same predicate :func:`forgeflow.mcp.auth.approved_grant`
    applies, duplicated here because this function sees the raw settings value
    (no tenant/role yet) and must not register a tool whose grant is unusable.
    """
    if not isinstance(grant, dict) or not isinstance(grant.get("read_only"), bool):
        return False
    return grant["read_only"] is True or grant.get("approval") == "per_call"


def install(definition: Any, target: Any):
    from forgeflow.runtime.tool_registry import ToolBinding, load_default_bindings, register

    name = definition.name
    grant = get_settings().mcp_tool_grants.get(name, {})
    if not _grantable(grant):
        return
    load_default_bindings()
    identifier = tool_id(name)
    parameters = copy.deepcopy(input_schema_of(definition))
    identity_fields = {"tenant", "tenant_id", "actor", "user_id", "role"}
    if identity_fields.intersection(parameters.get("required", [])):
        return
    for field in identity_fields:
        parameters.get("properties", {}).pop(field, None)
    schema = {
        "type": "function",
        "function": {
            "name": identifier,
            "description": (definition.description or name)[:2000],
            "parameters": parameters,
        },
    }
    _schemas[identifier] = schema
    schema_hash = hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()

    async def handler(args: dict, ctx: Any):
        from forgeflow.mcp.client.adapter import result_data
        from forgeflow.mcp.client.sessions import call_tool

        if (
            not ctx.tenant_id
            or not ctx.user_id
            or approved_grant(name, ctx.tenant_id, ctx.role) is None
        ):
            raise PermissionError("MCP identity is not authorized")
        from forgeflow.rbac.enforcer import RBACEnforcer

        if not RBACEnforcer().check(ctx.role, "execute", "workflows"):
            raise PermissionError("MCP runtime execution permission denied")
        if any(key in args for key in ("tenant", "tenant_id", "actor", "user_id", "role")):
            raise PermissionError("MCP identity cannot be supplied by model arguments")
        task = getattr(ctx, "task_context", None)
        if task is not None:
            expected = next(
                (
                    item
                    for item in task.context.get("_approved_tool_schemas", [])
                    if item["function"]["name"] == identifier
                ),
                None,
            )
            if expected is not None and expected != schema:
                raise ValueError("MCP tool schema changed; restart discovery for this task")
        ticket = None
        if grant["read_only"] is not True:
            # The write gate lives here, *before* dispatch: minting needs an
            # approved request, and the minted ticket is what the server burns.
            # Raising PermissionError means the executor records a refusal —
            # never a success with no side effect.
            from forgeflow.mcp.approvals import authorize_write

            ticket = await authorize_write(
                tenant=ctx.tenant_id,
                actor=ctx.user_id,
                role=ctx.role,
                run_id=str(getattr(ctx, "run_id", "") or ""),
                tool=name,
                arguments=args,
            )
        result = await call_tool(
            target, ctx.tenant_id, ctx.user_id, ctx.role, name, args, write_ticket=ticket
        )
        data = result_data(result)
        from forgeflow.security.tool_output_guard import sanitize_tool_output

        mcp_meta: dict[str, Any] = {
            "server": "internal",
            "tool": name,
            "schema_hash": schema_hash,
        }
        if ticket is not None:
            # Evidence that a specific human approval authorised this write.
            mcp_meta["write"] = True
            mcp_meta["approval_id"] = ticket.approval_id
        return {
            "ok": True,
            "summary": f"MCP {name} completed",
            "data": data,
            "model_text": sanitize_tool_output(name, data),
            "mcp": mcp_meta,
        }

    development = name.startswith(("crm_", "email_", "data_"))
    register(
        ToolBinding(
            identifier,
            handler,
            "development" if development else "real",
            "mcp:internal",
            definition.description or name,
        )
    )


def visible_tools(tenant: str, role: str) -> list[str]:
    from forgeflow.runtime.tool_executor import _dev_tools_allowed
    from forgeflow.runtime.tool_registry import resolve

    visible = []
    for identifier in sorted(_schemas):
        binding = resolve(identifier)
        if (
            binding is not None
            and approved_grant(identifier.removeprefix("mcp.internal."), tenant, role)
            and (binding.kind != "development" or _dev_tools_allowed())
        ):
            visible.append(identifier)
    return visible


def schema(identifier: str) -> dict | None:
    return _schemas.get(identifier)
