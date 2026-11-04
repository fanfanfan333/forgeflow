"""View masking in context_window.prepare_request (Phase 2).

When a request exceeds budget, old complete tool turns' observation
bodies are replaced by placeholders carrying an evidence receipt —
structure and pairing survive — and whole turns are dropped only when
masking alone cannot fit. Red lines pinned here: protected groups
(incomplete pairs, last two turns) are never masked, code.commit
observations are never masked, masking is idempotent, and the history
receipt always points at the full originals, never the masked view.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from forgeflow.config import get_settings
from forgeflow.runtime.context_window import (
    _group_turns,
    _mask_old_observations,
    prepare_request,
)
from forgeflow.runtime.professional_tools import evidence_read

MASK_MARKER = "[已掩码："


@pytest.fixture
def context(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "resource_store_root", str(tmp_path))
    monkeypatch.setattr(settings, "ollama_num_ctx", 4096)
    monkeypatch.setattr(settings, "agent_context_output_reserve", 1024)
    return SimpleNamespace(tenant_id="tenant-a", user_id="reader"), SimpleNamespace(context={})


def _turns(n: int, body: str = "verified source " * 150, tool: str = "document.inspect"):
    messages = [
        SystemMessage(content="do not change formulas"),
        HumanMessage(content="保留全部金额，完成两项要求"),
    ]
    for i in range(n):
        messages += [
            AIMessage(
                content="", tool_calls=[{"name": tool, "args": {}, "id": f"call-{i}"}]
            ),
            ToolMessage(content=body, tool_call_id=f"call-{i}"),
        ]
    return messages


def _tool_messages(selected):
    return [m for m in selected if isinstance(m, ToolMessage)]


@pytest.mark.asyncio
async def test_masking_avoids_dropping_and_stays_readable(context):
    ctx, task = context
    messages = _turns(10)
    selected, meta = prepare_request(messages, [], ctx, task)
    # Nothing dropped: all ten turns survive, pairs intact.
    assert len(_tool_messages(selected)) == 10
    for msg in selected:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            assert all(
                any(
                    isinstance(item, ToolMessage) and item.tool_call_id == call["id"]
                    for item in selected
                )
                for call in msg.tool_calls
            )
    # Old turns masked, last two turns full, no drop index message.
    masked = [m for m in _tool_messages(selected) if m.content.startswith(MASK_MARKER)]
    assert len(masked) == 8
    assert meta["masked_observations"] == 8
    assert meta["masked_turns"] == 8
    assert meta["compacted"] is True
    assert not any(
        isinstance(m, HumanMessage) and "Earlier complete tool turns" in str(m.content)
        for m in selected
    )
    for m in _tool_messages(selected)[-2:]:
        assert m.content == "verified source " * 150
    # The placeholder receipt reads back the full originals.
    ref = meta["history"]
    read = await evidence_read(
        {"ref": ref["evidence_ref"], "content_hash": ref["content_hash"]}, ctx
    )
    assert "verified source" in read["content"]


def test_code_commit_observations_are_never_masked(context):
    ctx, task = context
    messages = [
        SystemMessage(content="policy"),
        HumanMessage(content="do the task"),
        AIMessage(content="", tool_calls=[{"name": "code.commit", "args": {}, "id": "commit-0"}]),
        ToolMessage(content="diff content " * 100, tool_call_id="commit-0"),
    ]
    for i in range(7):
        messages += [
            AIMessage(content="", tool_calls=[{"name": "document.inspect", "args": {}, "id": f"call-{i}"}]),
            ToolMessage(content="verified source " * 150, tool_call_id=f"call-{i}"),
        ]
    selected, meta = prepare_request(messages, [], ctx, task)
    by_id = {m.tool_call_id: m for m in _tool_messages(selected)}
    assert by_id["commit-0"].content == "diff content " * 100
    assert meta["masked_observations"] == 5  # call-0..4 masked; last two full
    assert all(m.content.startswith(MASK_MARKER) for m in _tool_messages(selected) if m.tool_call_id not in {"commit-0", "call-5", "call-6"})


def test_masking_is_idempotent(context):
    ctx, task = context
    messages = _turns(10)
    selected, meta = prepare_request(messages, [], ctx, task)
    assert meta["masked_observations"] == 8
    masked_again, report, receipt = _mask_old_observations(
        selected, _group_turns(selected), ctx
    )
    assert report == []
    assert receipt is None
    assert masked_again == selected


@pytest.mark.asyncio
async def test_mask_then_drop_keeps_originals_as_history(context):
    ctx, task = context
    messages = _turns(40)
    selected, meta = prepare_request(messages, [], ctx, task)
    # Masking alone could not fit 40 turns: some turns were dropped,
    # but masking still ran and history still points at the originals.
    assert meta["masked_observations"] >= 1
    assert len(_tool_messages(selected)) < 40
    ref = meta["history"]
    read = await evidence_read(
        {"ref": ref["evidence_ref"], "content_hash": ref["content_hash"]}, ctx
    )
    assert "verified source" in read["content"]
