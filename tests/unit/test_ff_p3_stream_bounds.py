"""FF-R1 P3 — streaming is bounded: workflow streams time out, event
subscriptions cannot grow without limit.

Two surfaces:

* The workflow SSE stream (``stream_workflow``) obeys the
  server-configured ``workflow_run_timeout_seconds`` — ported from
  the snapshot branch (aee434b, ``test_stream_obeys_workflow_timeout``
  in ``test_remaining_agent_defects.py``), adapted only to main's
  ``stream_workflow`` signature (no ``pool``/``workspace_id`` params).
* The run event bus: a subscriber that stops consuming must not
  accumulate events forever (the queues were unbounded
  ``asyncio.Queue()``). The bound is a server setting; a consumer
  that overflows the bound is dropped AND its stream is ended with
  a ``subscriber_overflow`` error frame (P3-FIX: a dropped consumer
  whose stream hangs on keep-alives is not bounded in any sense
  that matters); disconnecting releases the subscription.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from forgeflow.runtime.events import RunEventBus

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Event bus bounds                                                             #
# --------------------------------------------------------------------------- #


async def test_slow_consumer_is_dropped_at_the_bound_and_never_blocks():
    bus = RunEventBus(queue_max=3)
    slow = await bus.subscribe("run-1")
    fast = await bus.subscribe("run-1")
    received = []
    for index in range(5):
        await bus.emit("run-1", "run.step", {"index": index})
        while not fast.empty():
            received.append(fast.get_nowait().data["index"])
    # The healthy consumer saw everything; emitting never blocked.
    assert received == [0, 1, 2, 3, 4]
    # The slow consumer's queue never exceeded the bound, and once it
    # overflowed it was dropped from the subscriber set entirely.
    assert slow.qsize() == 3
    assert slow not in bus._subscribers.get("run-1", [])


async def test_queue_bound_comes_from_the_server_setting_only():
    # P3-FIX (大川 ruling): there is no caller-requested queue size —
    # the parameter was removed rather than wired to the SSE path.
    # The bound is the server setting, full stop.
    from forgeflow.config import get_settings

    bus = RunEventBus(queue_max=4)
    assert (await bus.subscribe("r")).maxsize == 4
    default_bus = RunEventBus()
    assert (await default_bus.subscribe("r")).maxsize == get_settings().run_event_queue_max


async def test_overflowed_subscriber_stream_ends_with_an_error_frame():
    # Codex's P3 retest failure: a dropped subscriber's stream() used
    # to drain its queue and then hang on keep-alives forever — the
    # SSE connection was never released. The overflow must END the
    # stream, with an error frame naming the reason.
    bus = RunEventBus(queue_max=3)
    await bus.emit("run-9", "run.started", {})
    stream = bus.stream("run-9")
    first = await stream.__anext__()  # the subscription is live now
    assert "run.started" in first
    for index in range(5):
        await bus.emit("run-9", "run.step", {"index": index})

    frames: list[str] = []

    async def drain() -> None:
        async for frame in stream:
            frames.append(frame)

    await asyncio.wait_for(drain(), timeout=1.0)
    assert any("subscriber_overflow" in frame for frame in frames)


async def test_healthy_subscriber_stream_survives_another_consumers_overflow():
    bus = RunEventBus(queue_max=3)
    slow = await bus.subscribe("run-10")
    stream = bus.stream("run-10")
    await bus.emit("run-10", "run.started", {})
    assert "run.started" in await asyncio.wait_for(stream.__anext__(), timeout=1.0)
    received = []
    for index in range(5):
        await bus.emit("run-10", "run.step", {"index": index})
        frame = await asyncio.wait_for(stream.__anext__(), timeout=1.0)
        received.append(frame)
    # The healthy stream received every step and is still open; only
    # the slow consumer was dropped.
    assert len(received) == 5
    assert all("run.step" in frame for frame in received)
    assert slow not in bus._subscribers.get("run-10", [])
    with pytest.raises(RuntimeError, match="ignored GeneratorExit"):
        await stream.aclose()


async def test_history_preseed_is_truncated_to_the_newest_events():
    bus = RunEventBus(queue_max=3)
    for index in range(5):
        await bus.emit("run-2", "run.step", {"index": index})
    queue = await bus.subscribe("run-2")
    seeded = [queue.get_nowait().data["index"] for _ in range(queue.qsize())]
    assert seeded == [2, 3, 4]


async def test_disconnect_releases_the_subscription():
    bus = RunEventBus(queue_max=10)
    await bus.emit("run-3", "run.started", {})
    stream = bus.stream("run-3")
    first = await stream.__anext__()
    assert "run.started" in first
    assert bus._subscribers.get("run-3")
    # Closing the stream runs the generator's finally, which
    # unsubscribes BEFORE its final [DONE] yield; that yield makes
    # aclose() itself raise "async generator ignored GeneratorExit"
    # (pre-existing shape of the stream generator) — the release is
    # what this test pins, so the close error is expected noise here.
    with pytest.raises(RuntimeError, match="ignored GeneratorExit"):
        await stream.aclose()
    assert not bus._subscribers.get("run-3")


# --------------------------------------------------------------------------- #
# Workflow stream timeout (snapshot port, signature adapted)                   #
# --------------------------------------------------------------------------- #


async def test_stream_obeys_workflow_timeout(monkeypatch):
    from forgeflow.api.routers import workflows
    from forgeflow.config import get_settings

    class Pipeline:
        async def stream(self, *args, **kwargs):
            await asyncio.sleep(1)
            yield {"event": "workflow_complete"}

    monkeypatch.setattr(workflows, "_dispatch_pipeline", lambda *args: (Pipeline(), {}))
    monkeypatch.setattr(get_settings(), "workflow_run_timeout_seconds", 0.01)
    response = await workflows.stream_workflow(
        SimpleNamespace(workflow_type="sales_ops", lead_data={}, dry_run=True),
        graphs={"sales_ops": object()},
        user=SimpleNamespace(user_id="u", role="admin"),
    )
    chunks = [chunk async for chunk in response.body_iterator]
    assert any('"event": "error"' in chunk for chunk in chunks)
    assert not any('"event": "workflow_complete"' in chunk for chunk in chunks)
