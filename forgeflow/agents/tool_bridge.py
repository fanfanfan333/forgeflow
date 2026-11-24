"""FF-R1 B6 (FF-G12) — LangGraph agents execute tools via ToolExecutor.

The domain agents (``ExecutorAgent``, ``ResearcherAgent``) hold
LangChain ``BaseTool`` objects and used to invoke them directly
(``tool.ainvoke`` / the old ``_tool_data`` helper), bypassing the
platform's single execution choke point. Every agent tool call now
flows through :class:`forgeflow.runtime.tool_executor.ToolExecutor`,
so the unified pre-approval gate, fail-closed timeout, circuit
breaker, argument validation and trace all apply to the LangGraph
workflows exactly as they do to the runtime loops.

The bridge:

* registers a binding for the agent's tool (wrapping its ``ainvoke``)
  only when the runtime registry cannot already resolve the name —
  a platform-registered binding always wins;
* builds the :class:`ToolCallContext` from the workflow state
  (run = ``workflow_id``, user/role from ``run_metadata``, tenant
  from the state or the ambient tenant scope);
* translates the resulting :class:`ToolInvocation` back into the
  agents' dict-or-raise contract, sanitising every error and payload
  that crosses back (ToolExecutor records handler errors verbatim in
  its trace; nothing secret-shaped may reach agent logs or results).
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from langchain_core.tools import BaseTool

from forgeflow.connectors.email import redact_secrets
from forgeflow.runtime.approval_policy import APPROVAL_ARG_KEYS
from forgeflow.runtime.tool_executor import ToolCallContext, ToolExecutor
from forgeflow.runtime.tool_registry import ToolBinding, register, resolve
from forgeflow.tenant_scope import current_tenant

_SECRET_SHAPE = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|Bearer\s+\S+)")


class AgentToolError(RuntimeError):
    """A tool call through ToolExecutor did not produce a usable result."""

    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


class AgentToolApprovalRequired(AgentToolError):
    """The call is staged for human approval; nothing executed yet."""

    def __init__(self, pending_id: str | None) -> None:
        super().__init__(
            "awaiting_approval",
            "tool call is awaiting human approval and has not executed",
        )
        self.pending_id = pending_id


def _configured_secret_values() -> list[str]:
    """Configured secret values (names only matched, values never logged)."""
    values: list[str] = []
    try:
        from forgeflow.config import get_settings

        settings = get_settings()
        for name in dir(settings):
            lowered = name.lower()
            if not any(marker in lowered for marker in ("password", "secret", "token", "api_key")):
                continue
            value = getattr(settings, name, None)
            if hasattr(value, "get_secret_value"):
                value = value.get_secret_value()
            if isinstance(value, str) and len(value) >= 8:
                values.append(value)
    except Exception:  # noqa: BLE001 — redaction must never break a call
        return values
    return values


def sanitize_agent_value(value: Any) -> Any:
    """Recursively strip credential material from a tool result/error."""
    if isinstance(value, str):
        out = redact_secrets(value)
        out = _SECRET_SHAPE.sub("***", out)
        for secret in _configured_secret_values():
            out = out.replace(secret, "***")
        return out
    if isinstance(value, dict):
        return {key: sanitize_agent_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_agent_value(item) for item in value]
    return value


async def _agent_tool_handler(args: dict, ctx: Any) -> dict:
    """ToolBinding handler: invoke the BaseTool carried on the call context.

    The binding is registered once per tool NAME and must never capture
    a specific tool instance — agents are rebuilt per workflow (and per
    test), so the instance travels on ``ctx.runtime_context`` instead.
    """
    from langchain_core.messages import ToolMessage

    tool = getattr(ctx, "runtime_context", None)
    if tool is None or not hasattr(tool, "ainvoke") or not hasattr(tool, "name"):
        raise TypeError("agent tool binding invoked without its BaseTool on the context")
    clean_args = {k: v for k, v in dict(args).items() if k not in APPROVAL_ARG_KEYS}
    result = await tool.ainvoke(
        {"type": "tool_call", "id": uuid.uuid4().hex, "name": tool.name, "args": clean_args}
    )
    if isinstance(result, ToolMessage):
        result = result.artifact
    if not isinstance(result, dict):
        raise TypeError("MCP operation did not return a structured object")
    if "ok" not in result:
        # MCP payloads (e.g. HubSpot's ``{"id": …}``) carry no ``ok``
        # flag, but ToolExecutor classifies a missing one as a business
        # failure. Derive it from the signals the old direct-call path
        # used: an explicit ``success: False`` or an ``error`` member.
        result = {
            **result,
            "ok": result.get("success", True) is not False and not result.get("error"),
        }
    return result


def _ensure_binding(tool: BaseTool) -> None:
    if resolve(tool.name) is None:
        register(
            ToolBinding(
                tool_id=tool.name,
                handler=_agent_tool_handler,
                kind="real",
                provider="agent-bridge",
                description=f"LangGraph agent tool {tool.name} (FF-R1 B6 bridge)",
            )
        )


class _TaskShim:
    """Minimal task-context carrier for the approval gate's record scan.

    The gate treats same-run invocation records as a fast path only —
    the pending-action store claim is the authority — so a fresh,
    empty record list per call is correct.
    """

    def __init__(self) -> None:
        self.context: dict[str, Any] = {"tool_invocations": []}


async def execute_agent_tool(
    tool: BaseTool,
    args: dict,
    *,
    state: Any,
    step: str,
    approval_id: str | None = None,
) -> dict:
    """Execute one agent tool call through ToolExecutor.

    Returns the tool's structured payload on success. Raises
    :class:`AgentToolApprovalRequired` when the pre-approval gate
    staged the call, and :class:`AgentToolError` for every other
    non-success outcome — the agents' existing failure handling
    (``*_failed`` actions, "Tool error: …" feedback) applies unchanged.
    """
    _ensure_binding(tool)
    state_map: dict = state if isinstance(state, dict) else {}
    metadata = state_map.get("run_metadata") or {}
    run_id = str(state_map.get("workflow_id") or "agent-workflow")
    call_args = dict(args)
    if approval_id:
        # The gate reads the presented grant from the args; the key is
        # stripped before hashing and before the handler runs.
        call_args["pending_id"] = approval_id
    ctx = ToolCallContext(
        run_id=run_id,
        step_id=f"{run_id}:{step}",
        tenant_id=state_map.get("tenant_id") or current_tenant(),
        user_id=str(metadata.get("user_id") or "anonymous"),
        role=str(metadata.get("role") or "viewer"),
        intent="",
        args=call_args,
        runtime_context=tool,
        task_context=_TaskShim(),
    )
    invocation = await ToolExecutor().execute(tool.name, ctx=ctx, approval_id=approval_id)
    if invocation.status == "awaiting_approval":
        payload = invocation.payload if isinstance(invocation.payload, dict) else {}
        raise AgentToolApprovalRequired(payload.get("pending_id"))
    if invocation.status == "ok" and isinstance(invocation.payload, dict):
        payload = invocation.payload
        if payload.get("ok") is False or payload.get("error") or payload.get("mock") is True:
            raise AgentToolError(
                "error",
                sanitize_agent_value(str(payload.get("error") or "MCP operation failed")),
            )
        return sanitize_agent_value(payload)
    message = invocation.error or invocation.summary or f"tool call ended as {invocation.status}"
    raise AgentToolError(invocation.status, sanitize_agent_value(str(message)))
