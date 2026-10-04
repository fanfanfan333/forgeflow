"""RunEventBus — in-process pub/sub that backs the SSE execution stream.

Design (docs §4.1 ``GET /runs/{id}/events``):
  * every emitted event is stored in a per-run history ring (bounded) so a
    reconnect can be replayed;
  * subscribers get an ``asyncio.Queue`` and receive both the replayed history
    and any live events;
  * a terminal event (``run.completed`` / ``run.failed`` / ``run.aborted`` /
    ``done``) marks the stream finished, letting the SSE generator emit
    ``[DONE]`` and return.

Pure stdlib (``asyncio`` + ``json``) so it works with no external deps.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import AsyncGenerator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

TERMINAL_EVENTS = frozenset({"run.completed", "run.failed", "run.aborted", "done"})
_MAX_HISTORY = 200


@dataclass
class RunEvent:
    """One ordered step in a run's execution timeline."""

    run_id: str
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    seq: int = 0
    ts: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_sse(self) -> str:
        """Render as an SSE ``data:`` frame."""
        return f"data: {json.dumps(self.to_dict(), ensure_ascii=False)}\n\n"

    @property
    def terminal(self) -> bool:
        return self.type in TERMINAL_EVENTS


class RunEventBus:
    """Async broadcaster for run events."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._history: dict[str, list[RunEvent]] = {}
        self._counter = itertools.count(1)
        self._seq: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def emit(
        self, run_id: str, event_type: str, data: dict[str, Any] | None = None
    ) -> RunEvent:
        """Append an event to history and fan it out to subscribers."""
        async with self._lock:
            seq = self._seq.get(run_id, 0) + 1
            self._seq[run_id] = seq
            event = RunEvent(run_id=run_id, type=event_type, data=data or {}, seq=seq)
            history = self._history.setdefault(run_id, [])
            history.append(event)
            if len(history) > _MAX_HISTORY:
                del history[: len(history) - _MAX_HISTORY]
            for queue in list(self._subscribers.get(run_id, [])):
                queue.put_nowait(event)
            return event

    async def subscribe(self, run_id: str) -> asyncio.Queue:
        """Register a subscriber, pre-seeded with the run's history."""
        async with self._lock:
            queue: asyncio.Queue = asyncio.Queue()
            for event in self._history.get(run_id, []):
                queue.put_nowait(event)
            self._subscribers.setdefault(run_id, []).append(queue)
            return queue

    async def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        async with self._lock:
            subs = self._subscribers.get(run_id)
            if not subs:
                return
            if queue in subs:
                subs.remove(queue)
            if not subs:
                self._subscribers.pop(run_id, None)

    def history(self, run_id: str) -> list[RunEvent]:
        return list(self._history.get(run_id, []))

    async def stream(self, run_id: str) -> AsyncGenerator[str, None]:
        """Yield SSE frames for a run, ending with ``data: [DONE]``.

        If the run already finished, the replayed history ends with a terminal
        event so the generator closes immediately (reconnect-safe).
        """
        queue = await self.subscribe(run_id)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                except TimeoutError:
                    # Heartbeat keeps proxies from closing an idle stream.
                    yield ": keep-alive\n\n"
                    continue
                yield event.to_sse()
                if event.terminal:
                    break
        finally:
            await self.unsubscribe(run_id, queue)
            yield "data: [DONE]\n\n"


_BUS: RunEventBus | None = None


def get_event_bus() -> RunEventBus:
    """Process-wide singleton bus (shared by orchestrator + SSE router)."""
    global _BUS
    if _BUS is None:
        _BUS = RunEventBus()
    return _BUS


def reset_event_bus() -> None:
    """Replace the singleton. Test helper only."""
    global _BUS
    _BUS = RunEventBus()
