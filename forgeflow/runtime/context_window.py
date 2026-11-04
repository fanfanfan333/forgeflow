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


#: Marker prefix for masked tool observations (recognition + idempotency).
_MASK_MARKER = "[已掩码："

#: Tool names whose observations are never masked: a pending approval's
#: diff must stay visible to the model, not hidden behind a placeholder.
_NEVER_MASK_TOOLS = frozenset({"code.commit"})


def _group_turns(messages: list) -> list[list]:
    """Group messages into turns: an AI message plus its ToolMessages.

    Same grouping the over-capacity path uses: a ToolMessage joins the
    preceding AI-led group, anything else starts a new group.
    """
    groups: list[list] = []
    for message in messages:
        if isinstance(message, ToolMessage) and groups and isinstance(groups[-1][0], AIMessage):
            groups[-1].append(message)
        else:
            groups.append([message])
    return groups


def _mask_old_observations(messages: list, groups: list[list], ctx: Any) -> tuple[list, list[dict], dict | None]:
    """Mask old complete tool turns' observation bodies, keeping structure.

    View masking (OpenHands ``ObservationMaskingCondenser`` pattern):
    only the ToolMessage *body* is replaced by a placeholder carrying the
    original length and an evidence receipt — the AI call stub, the
    tool_call_id pairing and the turn order all survive, so the model
    still sees what was called and in which order, just not the full
    old output. The original messages are retained once via
    :func:`retain` and returned as the history receipt.

    Never masked: protected groups (incomplete tool-call pairs, or the
    last two groups — the same rule the drop path uses), non-Tool
    messages (system / human / AI content is never an observation),
    observations of :data:`_NEVER_MASK_TOOLS`, and bodies that are
    already masked (idempotent).

    Returns ``(masked_messages, report, history_receipt)``; the report
    is empty and the receipt is ``None`` when nothing was maskable.
    """
    tool_names: dict[str, str] = {}
    for message in messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls or []:
                if isinstance(call, dict) and call.get("id"):
                    tool_names[str(call["id"])] = str(call.get("name") or "")
    position = {id(m): i for i, m in enumerate(messages)}
    # First pass: decide what is maskable (positions, not equality — two
    # observations may share identical content but differ in call id).
    plan: list[tuple[int, ToolMessage, int, int]] = []
    for index, group in enumerate(groups):
        first = group[0]
        calls = (
            {call["id"] for call in first.tool_calls} if isinstance(first, AIMessage) else set()
        )
        results = {m.tool_call_id for m in group if isinstance(m, ToolMessage)}
        if not calls or calls != results or index >= len(groups) - 2:
            continue
        for message in group:
            if not isinstance(message, ToolMessage):
                continue
            if tool_names.get(str(message.tool_call_id), "") in _NEVER_MASK_TOOLS:
                continue
            content = message.content if isinstance(message.content, str) else ""
            if not content or content.startswith(_MASK_MARKER):
                continue
            plan.append((position[id(message)], message, len(content), index))
    if not plan:
        return list(messages), [], None
    # Retain the originals first, so the placeholder can carry a ref the
    # model can actually read back with evidence.read.
    receipt = retain(
        {"kind": "conversation_history", "messages": messages_to_dict(messages)}, ctx
    )
    masked = list(messages)
    report: list[dict] = []
    for pos, message, original_chars, turn in plan:
        masked[pos] = message.model_copy(
            update={
                "content": (
                    f"{_MASK_MARKER}原文 {original_chars} 字，"
                    f"证据 {receipt['evidence_ref']}，可用 evidence.read 回读]"
                )
            }
        )
        report.append(
            {
                "turn": turn,
                "tool_call_id": str(message.tool_call_id),
                "original_chars": original_chars,
                "evidence_ref": receipt["evidence_ref"],
                "content_hash": receipt["content_hash"],
            }
        )
    return masked, report, receipt


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
    masked_report: list[dict] = []
    if estimate > capacity:
        groups = _group_turns(messages)
        # Masking first: replace old observation bodies with placeholders
        # (structure + pairing survive) and only drop whole turns when
        # masking alone cannot bring the request within budget.
        masked_messages, masked_report, history_receipt = _mask_old_observations(
            messages, groups, ctx
        )
        if masked_report and estimate_tokens(serialized(masked_messages, schemas)) <= capacity:
            selected = masked_messages
            receipt = history_receipt
        else:
            messages = masked_messages
            groups = _group_turns(messages)
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
            if history_receipt is not None:
                # Full originals are the most complete history evidence;
                # skip the masked-history retain instead of duplicating it.
                receipt = history_receipt
            else:
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
    if masked_report:
        metadata["masked"] = masked_report
        metadata["masked_turns"] = len({entry["turn"] for entry in masked_report})
        metadata["masked_observations"] = len(masked_report)
    return selected, metadata
