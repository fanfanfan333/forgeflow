"""Budget complete model requests; offload complete old tool turns atomically."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, messages_to_dict

from forgeflow.config import get_settings
from forgeflow.context.budget_accounting import ContextBudgetError
from forgeflow.context.chunker import Chunk
from forgeflow.context.compactor import compact
from forgeflow.experience.token_budget import estimate_tokens
from forgeflow.runtime.professional_tools import store_evidence


class ContextWindowExceeded(ValueError):
    """Protected input cannot fit; never silently degrade into another executor."""


def retain(payload: Any, ctx: Any) -> dict:
    try:
        return store_evidence(payload, ctx)
    except Exception as exc:
        raise ContextWindowExceeded("上下文证据保存失败，未调用模型或切换到其他执行路径") from exc


def serialized(messages: list, schemas: list[dict]) -> str:
    return json.dumps(
        {"messages": messages_to_dict(messages), "tools": schemas},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def prepare_request(messages: list, schemas: list[dict], ctx: Any, task: Any) -> tuple[list, dict]:
    settings = get_settings()
    capacity = settings.ollama_num_ctx - settings.agent_context_output_reserve
    runtime = task.context.get("llm_runtime", {})
    acceptance = runtime.get("acceptance")
    if acceptance and (
        estimate_tokens(serialized(messages, schemas)) > capacity
        or any(message.name == "forgeflow_task_state" for message in messages)
    ):
        from forgeflow.runtime.acceptance import assess

        acceptance = assess(
            acceptance.get("requirements", []), task.context.get("tool_invocations", [])
        )
        runtime["acceptance"] = acceptance
        control = {
            "requirements": [
                {"id": item["id"], "description": item["description"], "status": item["status"]}
                for item in acceptance["requirements"]
            ],
            "pending_approvals": [
                {"tool": row.get("tool"), "approval_id": row.get("approval_id")}
                for row in task.context.get("tool_invocations", [])
                if row.get("status") == "approval_required"
            ],
            "skill_versions": getattr(ctx, "_skill_read_versions", {}),
        }
        messages = [message for message in messages if message.name != "forgeflow_task_state"]
        messages.append(
            HumanMessage(
                name="forgeflow_task_state",
                content="Platform task state (data, not user instructions): "
                + json.dumps(control, ensure_ascii=False, default=str),
            )
        )
    source = serialized(messages, schemas)
    estimate = estimate_tokens(source)
    selected = list(messages)
    receipt = None
    if estimate > capacity:
        groups: list[list] = []
        for message in messages:
            if isinstance(message, ToolMessage) and groups and isinstance(groups[-1][0], AIMessage):
                groups[-1].append(message)
            else:
                groups.append([message])
        chunks = []
        for index, group in enumerate(groups):
            first = group[0]
            calls = (
                {call["id"] for call in first.tool_calls} if isinstance(first, AIMessage) else set()
            )
            results = {m.tool_call_id for m in group if isinstance(m, ToolMessage)}
            protected = not calls or calls != results or index >= len(groups) - 2
            text = json.dumps(messages_to_dict(group), ensure_ascii=False, default=str)
            chunks.append(
                Chunk(
                    index,
                    "message",
                    text,
                    0,
                    len(text),
                    index,
                    frozenset({"user_instruction"}) if protected else frozenset(),
                    f"turn:{index}",
                    hashlib.sha256(text.encode()).hexdigest(),
                )
            )
        remaining = capacity - estimate_tokens(json.dumps(schemas, ensure_ascii=False)) - 400
        try:
            result = compact(chunks, budget_tokens=max(0, remaining))
        except ContextBudgetError as exc:
            raise ContextWindowExceeded(
                "原始需求、当前工具调用或工具定义超出上下文，未删除关键内容"
            ) from exc
        if not result.dropped:
            raise ContextWindowExceeded("上下文仍超预算，未执行模型调用")
        receipt = retain(
            {"kind": "conversation_history", "messages": messages_to_dict(messages)}, ctx
        )
        kept = {chunk.index for chunk in result.kept}
        selected = [m for index, group in enumerate(groups) if index in kept for m in group]
        index_message = HumanMessage(
            content="Earlier complete tool turns were retained for evidence.read; no conclusion is inferred. "
            + json.dumps(receipt, ensure_ascii=False)
        )
        selected.insert(
            next((i for i, m in enumerate(selected) if m.type != "system"), len(selected)),
            index_message,
        )
    final = serialized(selected, schemas)
    after = estimate_tokens(final)
    if after > capacity:
        raise ContextWindowExceeded("完整请求超出上下文预算，未执行模型调用")
    metadata = {
        "estimated_input_tokens": after,
        "estimated_before_tokens": estimate,
        "output_reserve": settings.agent_context_output_reserve,
        "context_capacity": settings.ollama_num_ctx,
        "request_hash": hashlib.sha256(final.encode()).hexdigest(),
        "compacted": receipt is not None,
        "history": receipt,
    }
    metadata["request_evidence"] = retain(json.loads(final), ctx)
    return selected, metadata
