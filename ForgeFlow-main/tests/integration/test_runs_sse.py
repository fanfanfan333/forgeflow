"""SSE integration — GET /runs/{id}/events streams data: frames + [DONE] (P0-12)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Test client with the graph + pool mocked (no PG / LLM)."""
    with patch("forgeflow.api.main.init_pool", new_callable=AsyncMock) as mock_pool, \
         patch("forgeflow.api.main.compile_graph", new_callable=AsyncMock) as mock_graph, \
         patch("forgeflow.api.main.get_mcp_tools", new_callable=AsyncMock, return_value=[]):
        from forgeflow.api.main import app

        mock_pool.return_value = MagicMock()
        mock_graph.return_value = MagicMock()
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


def _admin_headers() -> dict[str, str]:
    from forgeflow.auth.jwt import create_access_token

    return {"Authorization": f"Bearer {create_access_token(user_id='admin', role='admin')}"}


def test_runs_sse_streams_events_and_terminates(client):
    headers = _admin_headers()

    # Create a run first so there is a stream to read.
    resp = client.post("/tasks", json={"intent": "分析销售数据并生成报告"}, headers=headers)
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]

    # Read the SSE stream (the run already finished → history is replayed).
    with client.stream("GET", f"/runs/{run_id}/events", headers=headers) as stream:
        assert stream.status_code == 200
        assert stream.headers.get("content-type", "").startswith("text/event-stream")
        body = "".join(stream.iter_text())

    assert "data:" in body          # at least one frame
    assert "[DONE]" in body         # terminated cleanly
    assert "run.completed" in body or "run.started" in body


def test_runs_sse_requires_auth(client):
    resp = client.get("/runs/unknown-run/events")
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# INC-inline-streaming — the merged SSE outlet (step stream + token 旁路)        #
# --------------------------------------------------------------------------- #
# These tests are **additive**: the two cases above are byte-for-byte untouched.
import json as _json  # noqa: E402

from forgeflow.api.routers.runs import _merged_run_stream  # noqa: E402
from forgeflow.runtime.events import get_event_bus, reset_event_bus  # noqa: E402
from forgeflow.runtime.token_stream import (  # noqa: E402
    EVENT_TOKEN,
    EVENT_TURN,
    get_token_registry,
    reset_token_registry,
)


@pytest.fixture(autouse=True)
def _reset_token_registry():
    """Keep the process-wide token registry clean between SSE tests."""
    reset_token_registry()
    yield
    reset_token_registry()


def _create_run(client, intent: str = "分析销售数据并生成报告") -> tuple[str, dict]:
    headers = _admin_headers()
    resp = client.post("/tasks", json={"intent": intent}, headers=headers)
    assert resp.status_code == 200
    return resp.json()["run_id"], headers


def _read_sse(client, run_id: str, headers: dict) -> str:
    with client.stream("GET", f"/runs/{run_id}/events", headers=headers) as stream:
        assert stream.status_code == 200
        assert stream.headers.get("content-type", "").startswith("text/event-stream")
        return "".join(stream.iter_text())


def test_merged_stream_byte_identical_without_token_channel(client):
    """No token channel (mock 档) ⇒ 输出**逐字节等于**改动前的 ``bus.stream``。

    The pre-change endpoint returned exactly ``bus.stream(run_id)`` — every
    historical event's ``to_sse()`` in order, then ``data: [DONE]\\n\\n`` (with
    ``: keep-alive`` only on a >30s idle). Reconstructing that from
    ``bus.history`` is therefore the **baseline snapshot**; the merged outlet must
    match it byte-for-byte and open no token channel (C9 / AC-15).
    """
    run_id, headers = _create_run(client)
    body = _read_sse(client, run_id, headers)

    bus = get_event_bus()
    baseline = "".join(e.to_sse() for e in bus.history(run_id)) + "data: [DONE]\n\n"
    assert body == baseline
    # No token channel exists for a mock run ⇒ nothing was injected.
    assert get_token_registry().get(run_id) is None
    assert "run.token" not in body and "run.turn" not in body


def test_merged_stream_carries_token_and_step_frames(client):
    """token 帧与步骤帧均可解析、步骤帧格式不变、[DONE] 收尾在最后。

    A mock run opens **no** channel, so we drive one **by hand** (open → publish
    → close) purely to exercise the merged outlet. The run is already terminal, so
    the outlet drains the token sub-flow after the bus sub-flow and only then
    releases ``[DONE]`` — the收尾确定性 the whole feature depends on.
    """
    run_id, headers = _create_run(client)

    token = get_token_registry().open(run_id)
    token.publish(EVENT_TOKEN, {"turn": 0, "channel": "pending", "fragment": "答"})
    token.publish(EVENT_TOKEN, {"turn": 0, "channel": "pending", "fragment": "案"})
    token.publish(
        EVENT_TURN,
        {"turn": 0, "channel": "answer", "text": "答案", "interrupted": False},
    )
    token.close()

    body = _read_sse(client, run_id, headers)
    assert "data: [DONE]" in body

    frames = [
        _json.loads(line[len("data: ") :])
        for line in body.splitlines()
        if line.startswith("data: ") and "[DONE]" not in line
    ]
    types = [f["type"] for f in frames]

    # Both channels are present, and the step frames are still there.
    assert "run.token" in types and "run.turn" in types
    assert any(t not in ("run.token", "run.turn") for t in types)

    # Every frame keeps the (unchanged) step-event envelope.
    for f in frames:
        assert set(f.keys()) == {"run_id", "type", "data", "seq", "ts"}

    # Answer token frames arrive **before** ``[DONE]`` (收尾顺序钉子).
    head, _, _tail = body.partition("data: [DONE]")
    assert "run.token" in head and "run.turn" in head
    turns = [f for f in frames if f["type"] == "run.turn"]
    assert turns and turns[-1]["data"]["channel"] == "answer"


# --------------------------------------------------------------------------- #
# RD-1 / RD-4 — merged outlet robustness (mid-stream disconnect / DONE literal)  #
# --------------------------------------------------------------------------- #
async def test_merged_stream_disconnect_midrun_does_not_kill_token_channel():
    """RD-1: 客户端中途断开**不得** ``release`` 掉该 run 的 token 通道。

    ``release`` = pop + ``close()``；``close()`` 后 ``publish()`` 首行直接 return
    （静默全丢），而 executor 只在 ``run()`` 开头 ``open()`` 一次、永不重开 ⇒ 重连后
    只剩 step 事件（空洞等待）。故 ``finally`` 只在 run 终态时 release。
    """
    reset_token_registry()
    reset_event_bus()
    bus = get_event_bus()
    run_id = "r-rd1"
    await bus.emit(run_id, "run.started", {"intent": "x"})

    token = get_token_registry().open(run_id)
    agen = _merged_run_stream(run_id)
    async for _frame in agen:
        break                       # 中途断开（相当于客户端 aclose）
    await agen.aclose()

    stream = get_token_registry().get(run_id)
    assert stream is not None                 # RD-1: 未被回收
    assert stream.closed() is False           # 且未关闭 ⇒ publish 仍有效
    assert stream is token

    # 其后 publish 的帧必须能被**下一条连接**消费到（不是只断言"没抛异常"）。
    stream.publish(EVENT_TOKEN, {"turn": 0, "channel": "pending", "fragment": "重连后"})
    stream.publish(
        EVENT_TURN, {"turn": 0, "channel": "answer", "text": "重连后", "interrupted": False}
    )
    stream.close()
    await bus.emit(run_id, "run.completed", {"status": "completed"})

    body = ""
    async for frame in _merged_run_stream(run_id):
        body += frame
    assert "重连后" in body
    assert body.count("data: [DONE]") == 1


async def test_merged_stream_forwards_step_payload_containing_done_literal():
    """RD-4: ``[DONE]`` 子串匹配会误判 —— payload 含字面 ``[DONE]`` 的 step 不得被当终止帧。"""
    reset_token_registry()
    reset_event_bus()
    bus = get_event_bus()
    run_id = "r-rd4"
    await bus.emit(run_id, "run.step", {"note": "contains literal [DONE] token"})
    await bus.emit(run_id, "run.step", {"index": 1})
    await bus.emit(run_id, "run.completed", {"status": "completed"})

    body = ""
    async for frame in _merged_run_stream(run_id):
        body += frame
    assert "contains literal [DONE] token" in body   # 该帧被正常转发
    assert '"index": 1' in body                      # 其后的帧未被丢弃
    assert body.count("data: [DONE]") == 1           # 恰好一个终止帧

