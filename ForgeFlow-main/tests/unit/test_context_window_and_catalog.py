from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from forgeflow.config import get_settings
from forgeflow.runtime.context_window import ContextWindowExceeded, prepare_request
from forgeflow.runtime.professional_tools import evidence_read
from forgeflow.runtime.tool_catalog import search, search_handler


@pytest.fixture
def context(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "resource_store_root", str(tmp_path))
    monkeypatch.setattr(settings, "ollama_num_ctx", 4096)
    monkeypatch.setattr(settings, "agent_context_output_reserve", 1024)
    return SimpleNamespace(tenant_id="tenant-a", user_id="reader"), SimpleNamespace(context={})


def test_schema_budget_cannot_drop_user_instructions(context):
    ctx, task = context
    original = HumanMessage(content="保持金额和公式" * 5000)
    with pytest.raises(ContextWindowExceeded):
        prepare_request([SystemMessage(content="policy"), original], [], ctx, task)
    assert original.content == "保持金额和公式" * 5000


@pytest.mark.asyncio
async def test_complete_turns_offloaded_and_readback_is_scoped(context):
    ctx, task = context
    messages = [
        SystemMessage(content="do not change formulas"),
        HumanMessage(content="保留全部金额，完成两项要求"),
    ]
    for i in range(10):
        messages += [
            AIMessage(
                content="", tool_calls=[{"name": "document.inspect", "args": {}, "id": str(i)}]
            ),
            ToolMessage(content="verified source " * 200, tool_call_id=str(i)),
        ]
    selected, meta = prepare_request(messages, [], ctx, task)
    assert meta["compacted"] and meta["estimated_input_tokens"] <= 3072
    assert messages[0] in selected and messages[1] in selected
    for msg in selected:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            assert all(
                any(
                    isinstance(item, ToolMessage) and item.tool_call_id == call["id"]
                    for item in selected
                )
                for call in msg.tool_calls
            )
    ref = meta["history"]
    read = await evidence_read(
        {"ref": ref["evidence_ref"], "content_hash": ref["content_hash"]}, ctx
    )
    assert read["total_chars"] > 2000
    with pytest.raises(FileNotFoundError):
        await evidence_read(
            {"ref": ref["evidence_ref"]}, SimpleNamespace(tenant_id="tenant-b", user_id="reader")
        )


def test_unclosed_tool_call_is_not_removed(context):
    ctx, task = context
    pending = AIMessage(
        content="x" * 20000, tool_calls=[{"name": "read", "args": {}, "id": "pending"}]
    )
    with pytest.raises(ContextWindowExceeded):
        prepare_request([HumanMessage(content="original"), pending], [], ctx, task)


def test_failed_evidence_storage_stops_before_model_dispatch(context, monkeypatch):
    ctx, task = context
    from forgeflow.runtime import context_window

    def unavailable(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(context_window, "store_evidence", unavailable)
    with pytest.raises(ContextWindowExceeded, match="证据保存失败"):
        prepare_request([HumanMessage(content="original")], [], ctx, task)


@pytest.mark.asyncio
async def test_search_only_loads_the_approved_run_catalog():
    schemas = [
        {
            "type": "function",
            "function": {
                "name": "read.allowed",
                "description": "read document",
                "parameters": {"type": "object"},
            },
        }
    ]
    task = SimpleNamespace(context={"_approved_tool_schemas": schemas})
    ctx = SimpleNamespace(task_context=task)
    assert search(schemas, "payment.transfer") == []
    found = await search_handler({"query": "document"}, ctx)
    assert found["tools"] == schemas
    assert task.context["_loaded_tools"] == ["read.allowed"]
    with pytest.raises(ValueError):
        search(schemas, "", 3)
