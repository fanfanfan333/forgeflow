"""QA-INC32 · AC-34 / AC-35 / AC-36 — dispatcher Stop semantics (QA-authored).

The engineer's INC32 suites cover the abort/download HTTP surface + RBAC
(``tests/unit/test_inc32_workspace_api.py``) and the Follow-up context injection
(``tests/integration/test_inc32_followup_context.py``), but **no dedicated test
exercises the ``RunDispatcher`` itself** — i.e. the real machinery behind the
end-to-end ACs. This file pins the three end-to-end acceptance criteria at the
dispatcher boundary:

  * AC-34 — a Stop emits ``run.aborted`` (a terminal event) and the SSE stream
    therefore terminates;
  * AC-35 — Stop is terminal and irreversible (idempotent repeat; no ``resume``;
    the cancelled asyncio task cannot be revived);
  * AC-36 — after a Stop, **no further step / deliverable is produced**.

Deterministic: the background ``run_task`` is replaced by a fake that loops
emitting steps, so no LLM / DB is touched and the cancel race is fully driven by
the test. State is reset per test so it is order-independent.
"""

from __future__ import annotations

import asyncio

import pytest

import forgeflow.runtime.orchestrator as orch
from forgeflow.runtime.dispatcher import (
    get_run_dispatcher,
    is_run_cancelled,
    reset_run_dispatcher,
)
from forgeflow.runtime.events import get_event_bus, reset_event_bus
from forgeflow.runtime.orchestrator import (
    RequestContext,
    TaskCreate,
    get_run_store,
    reset_run_store,
)
from forgeflow.workspace.store import reset_workspace_store

TENANT = "t-inc32-disp"


@pytest.fixture(autouse=True)
def _clean_state():
    reset_run_store()
    reset_run_dispatcher()
    reset_event_bus()
    reset_workspace_store()
    yield
    reset_run_store()
    reset_run_dispatcher()
    reset_event_bus()
    reset_workspace_store()


def _ctx() -> RequestContext:
    return RequestContext(tenant_id=TENANT, user_id="u-disp", role="admin")


def _install_looping_run_task(monkeypatch, produced: list[int], ready: asyncio.Event):
    """Replace ``run_task`` with a background loop that 'produces' steps."""

    async def _fake_run_task(task, ctx, run_id=None, thread_id=None, **kwargs):
        bus = get_event_bus()
        ready.set()
        index = 0
        while True:  # cancelled by the dispatcher, never returns on its own
            index += 1
            produced.append(index)
            await bus.emit(run_id, "run.step", {"index": index})
            await asyncio.sleep(0.02)

    monkeypatch.setattr(orch, "run_task", _fake_run_task)


async def _collect_stream(run_id: str, *, limit: float = 2.0) -> list[str]:
    """Drain the SSE generator until it closes (or the guard times out)."""
    frames: list[str] = []

    async def _drain():
        async for frame in get_event_bus().stream(run_id):
            frames.append(frame)

    await asyncio.wait_for(_drain(), timeout=limit)
    return frames


# --------------------------------------------------------------------------- #
# AC-34 — Stop emits run.aborted (terminal) and the SSE stream terminates        #
# --------------------------------------------------------------------------- #
async def test_ac34_abort_emits_run_aborted_and_terminates_sse(monkeypatch):
    produced: list[int] = []
    ready = asyncio.Event()
    _install_looping_run_task(monkeypatch, produced, ready)

    dispatcher = get_run_dispatcher()
    handle = await dispatcher.dispatch(
        TaskCreate(intent="分析销售数据并生成报告", workflow_type="generic"), _ctx()
    )
    await asyncio.wait_for(ready.wait(), timeout=2.0)
    assert get_run_store().get(handle.run_id).status == "running"

    status = await dispatcher.abort(TENANT, handle.run_id)
    assert status == "aborted"

    # ``abort`` cancels the asyncio task; the ``run.aborted`` emit happens in the
    # cancelled task's handler, so let the loop process the cancellation first.
    await asyncio.sleep(0.15)

    # The terminating event is a real terminal event on the bus ...
    types = [e.type for e in get_event_bus().history(handle.run_id)]
    assert "run.aborted" in types
    # ... and the SSE stream therefore closes (terminal -> [DONE]).
    frames = await _collect_stream(handle.run_id)
    assert any("run.aborted" in f for f in frames), frames
    assert frames[-1].strip() == "data: [DONE]"
    assert get_run_store().get(handle.run_id).status == "aborted"


# --------------------------------------------------------------------------- #
# AC-35 — Stop is terminal and irreversible                                      #
# --------------------------------------------------------------------------- #
async def test_ac35_abort_is_terminal_and_irreversible(monkeypatch):
    produced: list[int] = []
    ready = asyncio.Event()
    _install_looping_run_task(monkeypatch, produced, ready)

    dispatcher = get_run_dispatcher()
    handle = await dispatcher.dispatch(
        TaskCreate(intent="分析销售数据并生成报告", workflow_type="generic"), _ctx()
    )
    await asyncio.wait_for(ready.wait(), timeout=2.0)

    assert await dispatcher.abort(TENANT, handle.run_id) == "aborted"
    # Idempotent repeat returns the same terminal value (200 in the route).
    assert await dispatcher.abort(TENANT, handle.run_id) == "aborted"
    # No resume surface exists at all.
    assert not hasattr(dispatcher, "resume") and not hasattr(dispatcher, "restart")
    record = get_run_store().get(handle.run_id)
    assert record.status == "aborted" and record.outcome == "aborted"


# --------------------------------------------------------------------------- #
# AC-36 — no further step / deliverable is produced after the Stop              #
# --------------------------------------------------------------------------- #
async def test_ac36_no_new_steps_after_abort(monkeypatch):
    produced: list[int] = []
    ready = asyncio.Event()
    _install_looping_run_task(monkeypatch, produced, ready)

    dispatcher = get_run_dispatcher()
    handle = await dispatcher.dispatch(
        TaskCreate(intent="分析销售数据并生成报告", workflow_type="generic"), _ctx()
    )
    await asyncio.wait_for(ready.wait(), timeout=2.0)

    # ``abort`` sets the cooperative flag *then* cancels the background task; the
    # CancelledError handler discards the flag as cleanup
    # (``dispatcher._run_and_finalize``). So the flag is only guaranteed set
    # while the cancellation is still in flight: reading it right after ``abort``
    # has returned is a race that the loop wins as soon as it gets a turn —
    # which happens sooner when ``abort`` awaits real persistence (PG profile) than
    # in memory. Neutralise that yield (non-yielding persistence) so the
    # cooperative-flag observation below is deterministic.
    async def _noop(_record):  # pragma: no cover - exercised via abort()
        return None

    monkeypatch.setattr(dispatcher, "_persist_workspace", _noop)

    assert await dispatcher.abort(TENANT, handle.run_id) == "aborted"
    # The cooperative flag the executors consult is set at abort time ...
    assert is_run_cancelled(handle.run_id) is True

    frozen = len(produced)
    # ... and no new step is produced once the cancel has propagated.
    await asyncio.sleep(0.3)
    assert len(produced) == frozen, (
        f"cancel did not stop production: {frozen} -> {len(produced)}"
    )
    record = get_run_store().get(handle.run_id)
    assert record.status == "aborted" and record.outcome == "aborted"
