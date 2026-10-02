"""Streaming ReAct tests — token pipeline, no-leak, suppression, cancel, F4, mock.

Design intent (E1 — honest disclosure): the real stack (qwen3:8b) does **not**
emit narration on a tool-calling turn (``content`` is measured empty), so the
narration typewriter is **not observable on the real stack**. This module proves
the **pipeline supports narration** with a **scripted stub model** whose
tool-calling turn carries non-empty ``content`` — and it never weakens an
assertion to make a criterion pass.

Covered (T02 criterion 2):
  ① scripted model: turn 0 has ``tool_calls`` + content ⇒ ``narration``; turn 1
     converges ⇒ ``answer``; run.token / run.turn are emitted;
  ② no-leak: injected ``tool_call_chunks`` / ``reasoning_content`` / ``thinking``
     never appear in any emitted frame (AC-14 / C2 / C3);
  ③ ``_looks_like_tool_syntax`` hit ⇒ the turn is ``suppressed`` and nothing more
     is emitted for it;
  ④ cancel: ``is_run_cancelled`` True ⇒ ``CancelledError`` raised **and** a
     ``run.turn{interrupted:true}`` was published (D7);
  ⑤ mock model ⇒ **no channel opened**, no token frames (C9);
  ⑥ F4 ruling — zero-frame failure degrades to ``bound_alt`` (legacy), but a
     failure **after** frames were emitted marks the turn interrupted and
     re-raises instead of splicing the (mock) alt output after a real half-answer.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

import forgeflow.runtime.orchestrator as orch
from forgeflow.config import get_settings
from forgeflow.runtime.orchestrator import (
    RequestContext,
    TaskCreate,
    reset_run_store,
    run_task,
)
from forgeflow.runtime.react_executor import (
    ReactExecutor,
    _content_text,
    _is_mock_model,
    _looks_like_tool_syntax,
    _TOOL_SYNTAX_MARKERS,
)
from forgeflow.runtime.token_stream import (
    EVENT_TOKEN,
    EVENT_TURN,
    get_token_registry,
    reset_token_registry,
)
from forgeflow.runtime.tool_registry import load_default_bindings, reset_registry

pytestmark = pytest.mark.asyncio


def _parse(frame: str) -> dict:
    assert frame.startswith("data: ")
    return json.loads(frame[len("data: ") :].strip())


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, run_id: str, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


class _ScriptedStreamBound:
    """A bound model exposing the real ``astream`` streaming surface."""

    def __init__(self, model: "_ScriptedStreamModel") -> None:
        self._model = model

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        return self._model._ainvoke_result(messages)

    async def astream(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self._model.calls.append(list(messages))
        for chunk in self._model._chunks_for(self._model._pop_turn()):
            yield chunk


class _ScriptedStreamModel:
    """Scripted chat model: each turn is a list of ``AIMessageChunk`` specs."""

    def __init__(self, turns: list[dict], *, llm_type: str = "fake") -> None:
        self._turns = [list(t.get("chunks", [])) for t in turns]
        self._llm_type = llm_type
        self.model = "fake-stream-model"
        self.calls: list[list] = []

    def bind_tools(self, tools):  # noqa: ANN001
        return _ScriptedStreamBound(self)

    def bind(self, **kwargs):  # noqa: ANN003
        return _ScriptedStreamBound(self)

    def _pop_turn(self) -> list[dict]:
        return self._turns.pop(0) if self._turns else [{"content": ""}]

    @staticmethod
    def _chunks_for(turn: list[dict]) -> list[AIMessageChunk]:
        chunks: list[AIMessageChunk] = []
        for spec in turn:
            chunks.append(
                AIMessageChunk(
                    content=spec.get("content", ""),
                    tool_call_chunks=spec.get("tool_call_chunks") or [],
                    additional_kwargs=spec.get("additional_kwargs") or {},
                )
            )
        return chunks

    def _ainvoke_result(self, messages) -> AIMessage:  # noqa: ANN001
        self.calls.append(list(messages))
        acc: AIMessageChunk | None = None
        for chunk in self._chunks_for(self._pop_turn()):
            acc = chunk if acc is None else acc + chunk
        return AIMessage(
            content=getattr(acc, "content", "") if acc is not None else "",
            tool_calls=list(getattr(acc, "tool_calls", None) or []),
        )


_UNSET = object()


class _SettingsProxy:
    def __init__(self, real, *, mode: object = _UNSET, provider: object = _UNSET) -> None:
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_mode", mode)
        object.__setattr__(self, "_provider", provider)

    def __getattr__(self, item: str):
        if item == "agent_runtime_mode" and self._mode is not _UNSET:
            return self._mode
        if item == "llm_provider" and self._provider is not _UNSET:
            return self._provider
        return getattr(self._real, item)


def _patch_settings(monkeypatch, *, mode="react", provider="mock") -> None:
    proxy = _SettingsProxy(get_settings(), mode=mode, provider=provider)
    monkeypatch.setattr(orch, "get_settings", lambda: proxy)


def _patch_models(monkeypatch, fake: _ScriptedStreamModel) -> None:
    monkeypatch.setattr(
        orch,
        "_build_planner_models",
        lambda: (fake, fake, [{"slot": "strong", "class": "_ScriptedStreamModel", "model": fake.model}]),
    )


@pytest.fixture(autouse=True)
def _clean_state():
    reset_run_store()
    reset_token_registry()
    yield
    reset_run_store()
    reset_token_registry()
    reset_registry()


@pytest.fixture(autouse=True)
def _bindings():
    load_default_bindings()
    yield


def _ctx(role: str = "admin", tenant: str = "t-react-stream") -> RequestContext:
    return RequestContext(tenant_id=tenant, user_id="u-1", role=role)


async def _drive(monkeypatch, fake: _ScriptedStreamModel) -> tuple[str, _RecordingBus]:
    _patch_settings(monkeypatch)
    _patch_models(monkeypatch, fake)
    task = TaskCreate(intent="检索并总结", workflow_type="generic", context={})
    bus = _RecordingBus()
    handle = await run_task(task, _ctx(), bus=bus)
    return handle.run_id, bus


async def _frames_for(run_id: str) -> list[dict]:
    stream = get_token_registry().get(run_id)
    assert stream is not None
    return [_parse(f) async for f in stream.frames()]


# --------------------------------------------------------------------------- #
# ① narration + answer through the full pipeline (E1)                            #
# --------------------------------------------------------------------------- #
async def test_pipeline_supports_narration_then_answer(monkeypatch, force_memory_backend):
    fake = _ScriptedStreamModel(
        [
            {
                "chunks": [
                    {"content": "正在检索…"},
                    {
                        "content": "",
                        "tool_call_chunks": [
                            {"name": "nope.tool", "args": "{}", "id": "c1", "index": 0}
                        ],
                    },
                ]
            },
            {"chunks": [{"content": "最终答案"}, {"content": "：完成"}]},
        ]
    )
    run_id, _bus = await _drive(monkeypatch, fake)
    frames = await _frames_for(run_id)

    tokens = [f for f in frames if f["type"] == EVENT_TOKEN]
    turns_frames = [f for f in frames if f["type"] == EVENT_TURN]

    t0 = [f for f in turns_frames if f["data"]["turn"] == 0]
    t1 = [f for f in turns_frames if f["data"]["turn"] == 1]
    assert t0 and t0[0]["data"]["channel"] == "narration"   # tool-calling turn
    assert t1 and t1[0]["data"]["channel"] == "answer"      # converged turn

    def _joined(turn: int) -> str:
        return "".join(
            f["data"]["fragment"] for f in tokens if f["data"]["turn"] == turn
        )

    assert _joined(0) == "正在检索…"
    assert _joined(1) == "最终答案：完成"                       # 前缀拼接，无造字
    # token 帧不带 turn 归类字段之外的内容；channel 恒为 pending（流式中未归类）。
    assert all(f["data"]["channel"] == "pending" for f in tokens)


# --------------------------------------------------------------------------- #
# ② no-leak — tool args / reasoning_content / thinking never emitted (AC-14)     #
# --------------------------------------------------------------------------- #
async def test_token_stream_never_leaks_tool_args_or_thinking(monkeypatch, force_memory_backend):
    fake = _ScriptedStreamModel(
        [
            {
                "chunks": [
                    {
                        "content": "正常文本",
                        "tool_call_chunks": [
                            {"name": "nope.tool", "args": '{"q":"SENTINEL_ARG"}', "id": "c1", "index": 0}
                        ],
                        "additional_kwargs": {
                            "reasoning_content": "SECRET_REASONING",
                            "thinking": "SECRET_THINK",
                        },
                    },
                ]
            },
            {"chunks": [{"content": "最终答案"}]},
        ]
    )
    run_id, _bus = await _drive(monkeypatch, fake)
    blob = json.dumps(await _frames_for(run_id), ensure_ascii=False)

    # 正面：content 确实外发了（否则下面的负断言是空的、不可证伪）。
    assert "正常文本" in blob
    # 反面：工具参数 JSON / reasoning_content / thinking 结构性不外发。
    for secret in ("SENTINEL_ARG", "SECRET_REASONING", "SECRET_THINK", "reasoning_content"):
        assert secret not in blob


# --------------------------------------------------------------------------- #
# ③ suppression on a疑似 tool-syntax marker                                     #
# --------------------------------------------------------------------------- #
async def test_tool_syntax_marker_suppresses_turn(monkeypatch, force_memory_backend):
    fake = _ScriptedStreamModel(
        [
            {
                "chunks": [
                    {"content": "正在"},                       # buffered, never flushed
                    {"content": '<tool_call>{"name":"nope.tool"}</tool_call>'},
                    {"content": "剩余泄漏内容"},
                    {
                        "content": "",
                        "tool_call_chunks": [
                            {"name": "nope.tool", "args": "{}", "id": "c1", "index": 0}
                        ],
                    },
                ]
            },
            {"chunks": [{"content": "最终答案"}]},
        ]
    )
    run_id, _bus = await _drive(monkeypatch, fake)
    frames = await _frames_for(run_id)

    t0_tokens = [
        f for f in frames if f["type"] == EVENT_TOKEN and f["data"]["turn"] == 0
    ]
    assert t0_tokens == []                                        # 该轮停发
    t0_turn = [f for f in frames if f["type"] == EVENT_TURN and f["data"]["turn"] == 0]
    assert t0_turn and t0_turn[0]["data"]["channel"] == "suppressed"
    assert t0_turn[0]["data"]["text"] == ""                       # 控制帧也不携带工具语法
    assert all("剩余泄漏内容" not in json.dumps(f, ensure_ascii=False) for f in frames)
    # 辅助断言：检测函数本身对四种标记都命中。
    assert _looks_like_tool_syntax("<tool_call>x")
    assert _looks_like_tool_syntax("```json")
    assert _looks_like_tool_syntax('"tool_calls"')
    assert not _looks_like_tool_syntax("普通答案文本")


# --------------------------------------------------------------------------- #
# ④ token-level cancel ⇒ CancelledError + run.turn{interrupted:true} (D7)        #
# --------------------------------------------------------------------------- #
async def test_astream_cancel_marks_interrupted_and_raises(monkeypatch):
    class _CancelBound:
        async def astream(self, messages):  # noqa: ANN001
            for i in range(40):  # 远超 _CANCEL_CHECK_CHUNKS=8
                yield AIMessageChunk(content=f"段{i} ")

    monkeypatch.setattr(
        "forgeflow.runtime.dispatcher.is_run_cancelled", lambda rid: True
    )
    stream = get_token_registry().open("r-cancel")
    with pytest.raises(asyncio.CancelledError):
        await ReactExecutor._astream(
            _CancelBound(), None, [], run_id="r-cancel", turn=0, stream=stream
        )
    stream.close()
    frames = [_parse(f) async for f in stream.frames()]

    assert any(f["type"] == EVENT_TOKEN for f in frames)          # 已外发部分 token
    turns = [f for f in frames if f["type"] == EVENT_TURN]
    assert turns and turns[-1]["data"]["interrupted"] is True
    assert turns[-1]["data"]["turn"] == 0
    # 检查点精度：恰在第 8 个 chunk（每 _CANCEL_CHECK_CHUNKS=8 一次）。
    interrupted = turns[-1]["data"]["text"]
    assert "段7" in interrupted and "段8" not in interrupted


# --------------------------------------------------------------------------- #
# ④b 真·per-chunk 流式分支证据（非退化分支）                                     #
# --------------------------------------------------------------------------- #
async def test_astream_true_per_chunk_streams_multiple_frames():
    """确证 scripted 桩**真的提供了 ``astream``** ⇒ 走 ``_astream`` 的 per-chunk 循环。

    30 个 × 2 字符 chunk：flush 阈值 ``_TOKEN_FLUSH_CHUNKS=6`` ⇒ 每 6 个 chunk 一帧，
    共 5 帧；且 Σ fragment == 全文（证明逐块外发 + 阈值合帧确实生效，不是退化分支）。
    """
    chunks = [AIMessageChunk(content="字字") for _ in range(30)]

    class _StreamBound:
        async def astream(self, messages):  # noqa: ANN001
            for c in chunks:
                yield c

    stream = get_token_registry().open("r-perchunk")
    result = await ReactExecutor._astream(
        _StreamBound(), None, [], run_id="r-perchunk", turn=0, stream=stream
    )
    assert _content_text(result) == "字字" * 30                  # 聚合 = 全文
    stream.close()
    frames = [_parse(f) async for f in stream.frames()]
    tokens = [f for f in frames if f["type"] == EVENT_TOKEN]
    assert len(tokens) == 5                                      # per-chunk 循环 + 阈值 flush
    assert "".join(f["data"]["fragment"] for f in tokens) == "字字" * 30


# --------------------------------------------------------------------------- #
# ⑤ mock model ⇒ no channel, no token frames (C9)                              #
# --------------------------------------------------------------------------- #
async def test_is_mock_model_recognises_stub():
    from forgeflow.models.provider import MockChatModel

    assert _is_mock_model(MockChatModel()) is True
    assert _is_mock_model(None) is True
    assert _is_mock_model(_ScriptedStreamModel([], llm_type="chat-ollama")) is False
    assert _is_mock_model(_ScriptedStreamModel([], llm_type="fake")) is False
    # fail-safe: 无法判定的对象当作 mock。
    class _Weird:
        @property
        def _llm_type(self):
            raise RuntimeError("nope")
    assert _is_mock_model(_Weird()) is True


async def test_mock_model_opens_no_channel(monkeypatch, force_memory_backend):
    fake = _ScriptedStreamModel([{"chunks": [{"content": "ok"}]}], llm_type="mock")
    run_id, _bus = await _drive(monkeypatch, fake)
    # mock 档不建通道 ⇒ SSE 出口无处可取 token 帧（输出逐字节不变，C9）。
    assert get_token_registry().get(run_id) is None


# --------------------------------------------------------------------------- #
# RD-2 / RD-3 — suppressed 不得在失败路径被绕过；中断路径只发 1 帧              #
# --------------------------------------------------------------------------- #
async def test_suppressed_cancel_publishes_single_empty_text_turn(monkeypatch):
    """一轮含工具语法 + 中途 cancel ⇒ (RD-3) 恰好 1 帧 run.turn、(RD-2) text==""."""
    class _SuppressCancelBound:
        async def astream(self, messages):  # noqa: ANN001
            for _ in range(20):
                yield AIMessageChunk(content='{"tool_calls": [{"name": "x"}]}')

    monkeypatch.setattr("forgeflow.runtime.dispatcher.is_run_cancelled", lambda rid: True)
    stream = get_token_registry().open("r-rd2-cancel")
    with pytest.raises(asyncio.CancelledError):
        await ReactExecutor._astream(
            _SuppressCancelBound(), None, [], run_id="r-rd2-cancel", turn=0, stream=stream
        )
    stream.close()
    frames = [_parse(f) async for f in stream.frames()]

    turns = [f for f in frames if f["type"] == EVENT_TURN]
    assert len(turns) == 1                                    # RD-3: 只发一帧
    assert turns[0]["data"]["text"] == ""                     # RD-2: 屏蔽不被绕过
    assert turns[0]["data"]["interrupted"] is True
    for f in frames:
        if f["type"] == EVENT_TOKEN:
            assert not any(m in f["data"]["fragment"] for m in _TOOL_SYNTAX_MARKERS)


async def test_suppressed_exception_after_frames_does_not_leak():
    """已外发常规文本后命中工具语法再抛异常 ⇒ (RD-2) run.turn.text 仍为 ""。"""
    class _SuppressThenFail:
        async def astream(self, messages):  # noqa: ANN001
            yield AIMessageChunk(content="正常答案文本" * 5)          # ≥24 字符 ⇒ flushed
            yield AIMessageChunk(content='```json {"tool_calls": 1}')
            raise RuntimeError("boom after frames")

    stream = get_token_registry().open("r-rd2-fail")
    with pytest.raises(RuntimeError):
        await ReactExecutor._astream(
            _SuppressThenFail(), None, [], run_id="r-rd2-fail", turn=0, stream=stream
        )
    stream.close()
    frames = [_parse(f) async for f in stream.frames()]

    turns = [f for f in frames if f["type"] == EVENT_TURN]
    assert turns and turns[-1]["data"]["interrupted"] is True
    assert turns[-1]["data"]["text"] == ""                    # RD-2
    tokens_text = "".join(
        f["data"]["fragment"] for f in frames if f["type"] == EVENT_TOKEN
    )
    assert "正常答案文本" in tokens_text                       # 命中前的常规文本已外发
    assert not any(m in tokens_text for m in _TOOL_SYNTAX_MARKERS)


# --------------------------------------------------------------------------- #
# ⑥ F4 ruling — zero-frame fallback vs frames-already-emitted                    #
# --------------------------------------------------------------------------- #
async def test_f4_zero_frames_degrades_to_alt(monkeypatch):
    class _FailsImmediately:
        async def astream(self, messages):  # noqa: ANN001
            raise RuntimeError("primary down before any chunk")
            yield  # pragma: no cover — makes this an async generator

    alt_model = _ScriptedStreamModel([{"chunks": [{"content": "worker 答复"}]}])
    alt = _ScriptedStreamBound(alt_model)
    stream = get_token_registry().open("r-f4-zero")

    result = await ReactExecutor._astream(
        _FailsImmediately(), alt, [], run_id="r-f4-zero", turn=0, stream=stream
    )
    assert _content_text(result) == "worker 答复"                  # legacy 降级语义不变
    stream.close()
    frames = [_parse(f) async for f in stream.frames()]
    assert any(
        f["type"] == EVENT_TOKEN and "worker 答复" in f["data"]["fragment"] for f in frames
    )
    assert not any(
        f["type"] == EVENT_TURN and f["data"].get("interrupted") for f in frames
    )


async def test_f4_after_frames_does_not_splice_alt(monkeypatch):
    partial = "真模型已外发的半截文本——" * 5  # > _TOKEN_FLUSH_CHARS ⇒ flushed

    class _PartialThenFail:
        async def astream(self, messages):  # noqa: ANN001
            yield AIMessageChunk(content=partial)
            raise RuntimeError("primary died after frames were emitted")

    alt_model = _ScriptedStreamModel([{"chunks": [{"content": "MOCK_ALT_TEXT"}]}])
    alt = _ScriptedStreamBound(alt_model)
    stream = get_token_registry().open("r-f4-partial")

    with pytest.raises(RuntimeError):
        await ReactExecutor._astream(
            _PartialThenFail(), alt, [], run_id="r-f4-partial", turn=0, stream=stream
        )
    stream.close()
    frames = [_parse(f) async for f in stream.frames()]
    blob = json.dumps(frames, ensure_ascii=False)

    assert partial in blob                                        # 已有帧外发（半截留痕）
    assert "MOCK_ALT_TEXT" not in blob                            # alt(mock) 未被拼接
    assert alt_model.calls == []                                  # bound_alt 根本未被调用
    assert any(
        f["type"] == EVENT_TURN and f["data"].get("interrupted") is True for f in frames
    )
