"""Unit tests for the token bypass channel (``forgeflow.runtime.token_stream``).

These pin the four hard contracts of the token channel (DESIGN §3.1):

  1. ``publish`` never blocks — a full queue coalesces / drops token frames;
  2. ``EVENT_TURN`` (control frame) is never dropped;
  3. ``close()`` makes ``frames()`` end (the sentinel always arrives);
  4. the registry reclaims closed streams and honours ``_MAX_RUNS``.

Plus the **reverse nail** (AC-16 / C1): a flood of token frames must **not**
enter ``RunEventBus._history`` (else the 452-frame answer would evict the
run's step events out of the 200-entry ring buffer).
"""

from __future__ import annotations

import json

import pytest

from forgeflow.runtime.events import RunEventBus
from forgeflow.runtime.token_stream import (
    EVENT_TOKEN,
    EVENT_TURN,
    CHANNEL_ANSWER,
    CHANNEL_PENDING,
    RunTokenStream,
    TokenStreamRegistry,
    get_token_registry,
    reset_token_registry,
)


def _parse(frame: str) -> dict:
    """Decode one SSE ``data:`` frame produced by the token channel."""
    assert frame.startswith("data: ")
    return json.loads(frame[len("data: ") :].strip())


async def _drain(stream: RunTokenStream) -> list[dict]:
    return [_parse(frame) async for frame in stream.frames()]


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_token_registry()
    yield
    reset_token_registry()


# --------------------------------------------------------------------------- #
# 1. publish never blocks — coalescing / dropping                                #
# --------------------------------------------------------------------------- #
async def test_full_queue_publish_never_blocks_and_coalesces():
    stream = RunTokenStream("r-coalesce", maxsize=8)
    # 1000 token frames into a maxsize=8 queue: must never raise / block, and the
    # internal buffer must stay bounded (memory upper bound, contract ④).
    for i in range(1000):
        stream.publish(EVENT_TOKEN, {"turn": 0, "channel": CHANNEL_PENDING, "fragment": "x"})
    assert len(stream._queue) <= 8

    # Same-turn/same-channel tail ⇒ coalesced (not 8 separate 1-char frames).
    stream.close()
    frames = await _drain(stream)
    assert len(frames) <= 8
    assert all(f["type"] == EVENT_TOKEN for f in frames)
    assert "".join(f["data"]["fragment"] for f in frames) == "x" * 1000  # nothing lost mid-tail

    # Different turns must NOT be coalesced into one another.
    stream2 = RunTokenStream("r-turns", maxsize=8)
    for turn in range(20):
        stream2.publish(EVENT_TOKEN, {"turn": turn, "channel": CHANNEL_PENDING, "fragment": "a"})
    assert len(stream2._queue) <= 8


async def test_full_queue_never_creates_holes_within_a_turn():
    """满队列下多次 publish ⇒ 每个 turn 的 Σ fragment 逐字等于喂进去的全部内容。

    Regression nail: an earlier implementation dropped the **oldest** token frame
    when full, carving a hole in the already-queued sequence (the consumer had not
    read those frames yet) ⇒ the frontend accumulated text lost characters,
    violating AC-3 (``conv-inline-answer`` monotone-prefix growth) and E2. The fix
    is **same-turn coalescing first** (lossless). Two interleaved turns guarantee a
    same-turn pair always exists, so nothing may be lost.
    """
    stream = RunTokenStream("r-nohole", maxsize=4)
    n = 200
    for _ in range(n):
        stream.publish(EVENT_TOKEN, {"turn": 0, "channel": CHANNEL_PENDING, "fragment": "a"})
        stream.publish(EVENT_TOKEN, {"turn": 1, "channel": CHANNEL_PENDING, "fragment": "b"})
    assert len(stream._queue) <= 4

    stream.close()
    frames = await _drain(stream)
    by_turn: dict[int, str] = {}
    for f in frames:
        if f["type"] == EVENT_TOKEN:
            turn = f["data"]["turn"]
            by_turn[turn] = by_turn.get(turn, "") + f["data"]["fragment"]
    # 逐字无损、无乱序（顺序不可反）。
    assert by_turn.get(0) == "a" * n
    assert by_turn.get(1) == "b" * n


# --------------------------------------------------------------------------- #
# 2. EVENT_TURN survives a saturated queue                                       #
# --------------------------------------------------------------------------- #
async def test_control_frame_survives_saturated_queue():
    stream = RunTokenStream("r-control", maxsize=4)
    for _ in range(200):
        stream.publish(EVENT_TOKEN, {"turn": 0, "channel": CHANNEL_PENDING, "fragment": "y"})
    stream.publish(
        EVENT_TURN,
        {"turn": 0, "channel": CHANNEL_ANSWER, "text": "final", "interrupted": False},
    )
    # Still-bounded, and the control frame is present and last.
    assert len(stream._queue) <= 4
    stream.close()
    frames = await _drain(stream)
    turns = [f for f in frames if f["type"] == EVENT_TURN]
    assert len(turns) == 1
    assert turns[0]["data"] == {
        "turn": 0,
        "channel": CHANNEL_ANSWER,
        "text": "final",
        "interrupted": False,
    }
    assert frames[-1]["type"] == EVENT_TURN  # control frame is the tail (never dropped)


async def test_control_frames_never_dropped_even_when_control_flooded():
    stream = RunTokenStream("r-many-turns", maxsize=3)
    for turn in range(50):
        stream.publish(
            EVENT_TURN,
            {"turn": turn, "channel": CHANNEL_ANSWER, "text": str(turn), "interrupted": False},
        )
    stream.close()
    frames = await _drain(stream)
    turns = [f for f in frames if f["type"] == EVENT_TURN]
    # Every control frame survives (token 帧可丢，控制帧不可丢).
    assert [t["data"]["turn"] for t in turns] == list(range(50))


# --------------------------------------------------------------------------- #
# 3. close() ⇒ frames() ends                                                     #
# --------------------------------------------------------------------------- #
async def test_close_ends_frames_and_is_idempotent():
    stream = RunTokenStream("r-close")
    stream.publish(EVENT_TOKEN, {"turn": 0, "channel": CHANNEL_PENDING, "fragment": "a"})
    stream.close()
    stream.close()  # idempotent — second call must not enqueue a second sentinel

    frames = await _drain(stream)
    assert len(frames) == 1
    assert stream.closed() is True

    # A closed stream swallows further publishes (no frames after the sentinel).
    stream.publish(EVENT_TOKEN, {"turn": 0, "channel": CHANNEL_PENDING, "fragment": "b"})
    assert await _drain(stream) == []


async def test_frames_stops_immediately_for_empty_closed_stream():
    stream = RunTokenStream("r-empty")
    stream.close()
    assert await _drain(stream) == []


# --------------------------------------------------------------------------- #
# 4. registry — reclaim + _MAX_RUNS ceiling                                      #
# --------------------------------------------------------------------------- #
async def test_release_reclaims_stream():
    registry = TokenStreamRegistry()
    stream = registry.open("r-1")
    assert registry.get("r-1") is stream
    registry.release("r-1")
    assert registry.get("r-1") is None
    assert stream.closed() is True
    registry.release("r-1")  # idempotent (already gone)


async def test_open_after_close_returns_fresh_stream():
    registry = TokenStreamRegistry()
    first = registry.open("r-1")
    first.close()
    second = registry.open("r-1")
    assert second is not first
    assert second.closed() is False


async def test_max_runs_ceiling_reclaims_oldest():
    registry = TokenStreamRegistry(max_runs=3)
    streams = [registry.open(f"r-{i}") for i in range(3)]
    # All are still open (not closed) — the ceiling must still hold.
    registry.open("r-new")
    assert len(registry._streams) == 3
    # The oldest open one was evicted; the newest is present.
    assert registry.get("r-0") is None
    assert registry.get("r-new") is not None
    # Eviction closes what it drops.
    assert streams[0].closed() is True


async def test_max_runs_prefers_reclaiming_closed():
    registry = TokenStreamRegistry(max_runs=2)
    a = registry.open("r-a")
    registry.open("r-b")
    a.close()  # closed streams are reclaimed first (no open run is disturbed)
    registry.open("r-c")
    assert registry.get("r-a") is None
    assert registry.get("r-b") is not None
    assert registry.get("r-c") is not None


# --------------------------------------------------------------------------- #
# 5. Reverse nail — token frames never enter the bus ring (AC-16 / C1)           #
# --------------------------------------------------------------------------- #
async def test_token_frames_never_enter_bus_history_and_steps_not_evicted():
    """A 452-frame answer must not push step events out of ``_history``.

    Measured reality (team-lead brief): under ``think=true`` one answer is 452
    token frames while ``RunEventBus._MAX_HISTORY`` is only 200. If token frames
    shared that ring, the run's ``run.started`` / ``run.step`` / ``run.observation``
    would be evicted wholesale. Keeping them on a **separate** channel is the
    whole point of this module, so this test is the falsifiable proof.
    """
    bus = RunEventBus()
    run_id = "run-ac16"

    # 100 step events (< _MAX_HISTORY) — they must ALL survive.
    for i in range(100):
        await bus.emit(run_id, "run.step", {"index": i})

    stream = RunTokenStream(run_id)
    for _ in range(452):  # the 452-frame answer
        stream.publish(EVENT_TOKEN, {"turn": 0, "channel": CHANNEL_PENDING, "fragment": "字"})
    stream.publish(
        EVENT_TURN,
        {"turn": 0, "channel": CHANNEL_ANSWER, "text": "字" * 452, "interrupted": False},
    )

    history = bus.history(run_id)
    # No token / turn event leaked into the step ring.
    assert all(e.type not in ("run.token", "run.turn") for e in history)
    # Every step event survived — nothing was evicted.
    assert [e.type for e in history] == ["run.step"] * 100
    assert [e.data["index"] for e in history] == list(range(100))

    # The 452 frames live only in the token channel, bounded by _MAX_QUEUE.
    assert len(stream._queue) <= 64
