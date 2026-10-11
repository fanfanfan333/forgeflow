"""RunEventBus — in-process pub/sub that backs the SSE execution stream.

Design (docs §4.1 ``GET /runs/{id}/events``):
  * every emitted event is stored in a per-run history ring (bounded) so a
    reconnect can be replayed;
  * subscribers get an ``asyncio.Queue`` and receive both the replayed history
    and any live events;
  * a terminal event (``run.completed`` / ``run.failed`` / ``run.aborted`` /
    ``done``) marks the stream finished, letting the SSE generator emit
    ``[DONE]`` and return.

INC-multinode (additive): an **optional** :class:`RunEventLog` may be injected.
When present, every emitted event is *best-effort* persisted and a reconnect may
supply a ``last_event_id`` cursor to replay ``seq > cursor`` from the durable log.
When ``log is None`` the behaviour is **byte-for-byte** the pre-change behaviour.

Pure stdlib (``asyncio`` + ``json``) so it works with no external deps; the log
itself is the only optional collaborator.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from collections.abc import AsyncGenerator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from forgeflow.runtime.event_log import RunEventLog, get_event_log

logger = logging.getLogger(__name__)

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


def _run_event_from_row(row: dict[str, Any]) -> RunEvent:
    """Rebuild a :class:`RunEvent` from a persisted log row.

    The row shape is the documented ``{"run_id", "seq", "type", "data", "ts"}``;
    ``ts`` may arrive as a ``str`` (JSON log) or ``datetime`` (Postgres).
    """
    ts = row.get("ts")
    if isinstance(ts, datetime):
        ts = ts.isoformat()
    return RunEvent(
        run_id=str(row.get("run_id", "")),
        type=str(row.get("type", "")),
        data=dict(row.get("data") or {}),
        seq=int(row.get("seq", 0)),
        ts=str(ts) if ts else datetime.now(UTC).isoformat(),
    )


class RunEventBus:
    """Async broadcaster for run events."""

    def __init__(self, log: RunEventLog | None = None) -> None:
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._history: dict[str, list[RunEvent]] = {}
        self._counter = itertools.count(1)
        self._seq: dict[str, int] = {}
        self._lock = asyncio.Lock()
        #: Optional durable log. ``None`` ⇒ today's in-memory-only behaviour.
        self._log = log

    def _ensure_seq(self, run_id: str) -> int:
        """Next ``seq`` for ``run_id``.

        With no log this is exactly ``self._seq.get(run_id, 0) + 1`` (unchanged).
        With a log, the first call for a run continues from the persisted
        ``max_seq(run_id)`` so a restart (or another instance) never re-issues a
        colliding sequence number. Must be called under ``self._lock``.
        """
        current = self._seq.get(run_id)
        if current is None:
            base = 0
            if self._log is not None:
                try:
                    base = int(self._log.max_seq(run_id))
                except Exception as exc:  # noqa: BLE001 — never fail the run
                    logger.warning("event log max_seq failed for run %s: %s", run_id, exc)
                    base = 0
            current = base
        nxt = current + 1
        self._seq[run_id] = nxt
        return nxt

    async def emit(
        self, run_id: str, event_type: str, data: dict[str, Any] | None = None
    ) -> RunEvent:
        """Append an event to history and fan it out to subscribers.

        Persistence (when a log is injected) is **best-effort**: a logging
        failure is swallowed with a warning and can never fail the run.
        """
        async with self._lock:
            seq = self._ensure_seq(run_id)
            event = RunEvent(run_id=run_id, type=event_type, data=data or {}, seq=seq)
            history = self._history.setdefault(run_id, [])
            history.append(event)
            if len(history) > _MAX_HISTORY:
                del history[: len(history) - _MAX_HISTORY]
            for queue in list(self._subscribers.get(run_id, [])):
                queue.put_nowait(event)
        # Best-effort durable write, OUTSIDE the lock (a slow/blocking DB write
        # must not stall other emits). A failure is warned, never re-raised.
        if self._log is not None:
            try:
                self._log.append(run_id, event.seq, event.type, event.data)
            except Exception as exc:  # noqa: BLE001 — logging must not fail the run
                logger.warning(
                    "event log append failed for run %s seq %s: %s",
                    run_id,
                    event.seq,
                    exc,
                )
        return event

    async def subscribe(self, run_id: str) -> asyncio.Queue:
        """Register a subscriber, pre-seeded with the run's history."""
        async with self._lock:
            queue: asyncio.Queue = asyncio.Queue()
            for event in self._history.get(run_id, []):
                queue.put_nowait(event)
            self._subscribers.setdefault(run_id, []).append(queue)
            return queue

    async def _subscribe_empty(self, run_id: str) -> asyncio.Queue:
        """Register a subscriber **without** pre-seeding in-memory history.

        Used by the cursor path: the durable log supplies the backfill, so the
        in-memory ring must not double-deliver it.
        """
        async with self._lock:
            queue: asyncio.Queue = asyncio.Queue()
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

    async def stream(
        self, run_id: str, last_event_id: str | None = None
    ) -> AsyncGenerator[str, None]:
        """Yield SSE frames for a run, ending with ``data: [DONE]``.

        * ``log is None`` or ``last_event_id is None`` ⇒ the pre-change path
          (subscribe + pre-seeded in-memory history), byte-for-byte unchanged.
        * otherwise: register a live subscriber first (so events emitted after
          this point land in the queue), then replay ``seq > last_event_id`` from
          the durable log, then drain the queue **skipping any seq already sent**
          ⇒ union covers everything, de-duplicated by ``seq`` (no gap, no dup).
        """
        cursor: int | None = None
        if self._log is not None and last_event_id is not None:
            try:
                cursor = int(last_event_id)
            except (TypeError, ValueError):
                cursor = None

        if cursor is None:
            queue = await self.subscribe(run_id)
        else:
            queue = await self._subscribe_empty(run_id)

        last_sent = cursor if cursor is not None else -1
        try:
            if cursor is not None:
                # Backfill from the durable log BEFORE the live drain. The
                # subscription was already registered above, so anything emitted
                # in the meantime is queued and delivered next (deduped by seq).
                try:
                    rows = self._log.read(run_id, after_seq=cursor)  # type: ignore[union-attr]
                except Exception as exc:  # noqa: BLE001 — read failure degrades to live-only
                    logger.warning("event log read failed for run %s: %s", run_id, exc)
                    rows = []
                for row in rows:
                    event = _run_event_from_row(row)
                    if event.seq <= last_sent:
                        continue
                    last_sent = event.seq
                    yield event.to_sse()
                    if event.terminal:
                        return
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                except TimeoutError:
                    # Heartbeat keeps proxies from closing an idle stream.
                    yield ": keep-alive\n\n"
                    continue
                if cursor is not None and event.seq <= last_sent:
                    # Already delivered by the backfill — skip (no duplicate).
                    # A *terminal* duplicate must still close the stream: when the
                    # durable copy of a terminal event was lost (persistence is
                    # best-effort) but the in-memory history still holds it,
                    # skipping it outright would leave the client hanging on
                    # keep-alives forever instead of reaching ``[DONE]``.
                    if event.terminal:
                        break
                    continue
                last_sent = event.seq
                yield event.to_sse()
                if event.terminal:
                    break
        finally:
            await self.unsubscribe(run_id, queue)
            yield "data: [DONE]\n\n"


_BUS: RunEventBus | None = None


def get_event_bus() -> RunEventBus:
    """Process-wide singleton bus (shared by orchestrator + SSE router).

    A durable :class:`RunEventLog` is injected when one is configured (postgres
    profile); the offline profile yields ``None`` ⇒ identical behaviour.
    """
    global _BUS
    if _BUS is None:
        _BUS = RunEventBus(log=get_event_log())
    return _BUS


def reset_event_bus() -> None:
    """Replace the singleton. Test helper only."""
    global _BUS
    _BUS = RunEventBus(log=get_event_log())
