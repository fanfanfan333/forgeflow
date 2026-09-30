"""INC17 — the shared replan write-back: ``record.steps`` is the LAST attempt.

This file pins a **one-line, deliberate consistency fix** that INC17 made in the
shared ``run_task`` replan loop (``forgeflow/runtime/orchestrator.py``)::

    steps_retry, errors = await retry_executor(...)
    steps = steps_retry          # <-- INC17: keep ``steps`` in step with the attempt
    run_state["steps"] = steps_retry

Why it is a *fix*, not a tweak
------------------------------
Before INC17 the module-local ``steps`` was **never** re-assigned inside the
replan loop: ``errors`` and ``run_state["steps"]`` were already the **last**
attempt's view, but the persisted ``RunRecord.steps`` (and
``RunHandle.detail["steps"]``) were still the **first** attempt's. The record was
therefore internally inconsistent — its ``steps`` and its ``errors`` described
* different attempts*. That latent bug also made the INC17 run-level 1:1 contract
(design §11-C: ``steps`` == cumulative ``tool_invocations`` for the ``react``
runtime) impossible to satisfy, because a cumulative ``steps`` from the react
executor could never reach the record.

Measurement scope (declared, per INC17 discipline)
--------------------------------------------------
* **``react`` mode** — the react executor returns a **cumulative** ``steps``
  (every replan round's records), so after this fix both ``record.steps`` and the
  cumulative ``tool_invocations`` are cumulative ⇒ **run-level 1:1 is claimed for
  ``react``** (pinned in ``tests/integration/test_inc17_react_four_layer.py``).
* **``llm`` / ``deterministic`` / ``graph`` modes** — their executors return a
  **single-attempt** ``steps``. After this fix ``record.steps`` is still a
  *single-attempt* view while ``tool_invocations`` is the **cumulative** trail;
  the two are deliberately **NOT equal** across a replan. **Run-level 1:1 is NOT
  claimed for these modes.** This divergence pre-dates INC17 (it is the documented
  ``§10`` finding) and is **not** fixed here — only its *steps/errors* internal
  inconsistency is fixed, and the remaining divergence is stated honestly.

The test drives the **offline ``deterministic`` runtime** (``STORAGE_BACKEND=memory``
+ ``LLM_PROVIDER=mock`` via ``force_memory_backend``) with the real executor
wrapped so that **attempt 0 reports an error** (forcing exactly one replan) and
**attempt 1 succeeds** — the "executor reports failure ⇒ the replan loop runs"
construct used by ``test_runs_api.py`` / ``test_inc12_llm_executor_real.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

import forgeflow.runtime.orchestrator as orch
from forgeflow.runtime.orchestrator import (
    RequestContext,
    TaskCreate,
    get_run_store,
    reset_run_store,
    resolve_agent_runtime_mode,
    run_task,
)

pytestmark = pytest.mark.asyncio


class _RecordingBus:
    """Minimal bus stub — ``run_task`` / the executor only call ``emit``."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, run_id: str, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


@pytest.fixture(autouse=True)
def _clean_state():
    reset_run_store()
    yield
    reset_run_store()


def _ctx() -> RequestContext:
    return RequestContext(tenant_id="t-inc17-writeback", user_id="u-1", role="admin")


def _wrap_real_executor(
    monkeypatch: pytest.MonkeyPatch,
    captured_steps: dict[int, list[dict[str, Any]]],
    captured_errors: dict[int, list[str]],
    *,
    fail_first: bool,
) -> None:
    """Wrap the real deterministic executor to (a) capture each attempt's returned
    ``(steps, errors)`` and (b) optionally make **attempt 0** report an error so
    the run really enters the replan loop. The executor itself is unmodified."""
    real = orch._default_executor

    async def _exec(
        task: Any, ctx: Any, bus: Any, run_id: str, *, policy_engine: Any = None, attempt: int = 0
    ) -> tuple[list[dict[str, Any]], list[str]]:
        steps, errors = await real(
            task, ctx, bus, run_id, policy_engine=policy_engine, attempt=attempt
        )
        captured_steps[attempt] = steps
        if fail_first and attempt == 0:
            # A real, non-empty error ⇒ ``validate`` fails ⇒ ``decide_replan``
            # authorises one retry (the second attempt then succeeds).
            errors = ["夹具：首次尝试强制失败 ⇒ 必须走一次 replan"]
        captured_errors[attempt] = errors
        return steps, errors

    monkeypatch.setattr(orch, "_default_executor", _exec)


def _attempt_segment(steps: list[dict[str, Any]]) -> str:
    """The ``{attempt}`` segment of the stable ``{run_id}:{attempt}:{index}`` id."""
    assert steps, "a recorded attempt must carry at least one step"
    parts = str(steps[0]["step_id"]).split(":")
    assert len(parts) >= 3, steps[0]["step_id"]
    return parts[-2]


# --------------------------------------------------------------------------- #
# 1. With a replan: record.steps + record.errors describe the LAST attempt      #
# --------------------------------------------------------------------------- #
async def test_record_steps_reflect_the_last_attempt_after_a_replan(
    monkeypatch, force_memory_backend
):
    # The offline profile really resolves to the deterministic runtime.
    assert resolve_agent_runtime_mode() == "deterministic"

    captured_steps: dict[int, list[dict[str, Any]]] = {}
    captured_errors: dict[int, list[str]] = {}
    _wrap_real_executor(monkeypatch, captured_steps, captured_errors, fail_first=True)

    handle = await run_task(
        TaskCreate(intent="离线分析一次并触发一次重规划", workflow_type="generic"),
        _ctx(),
        bus=_RecordingBus(),
    )
    record = get_run_store().get(handle.run_id)
    assert record is not None

    # A replan really happened: attempts 0 and 1 both ran, and their step lists
    # are distinguishable (the step_id carries the attempt segment).
    assert set(captured_steps) == {0, 1}
    assert captured_steps[0] != captured_steps[1]
    assert captured_errors[0], "attempt 0 must have failed (fixture)"
    assert captured_errors[1] == [], "attempt 1 must have succeeded"
    last = max(captured_steps)

    # The record (and the handle) carry the LAST attempt's steps — NOT the first's.
    assert record.steps == captured_steps[last]
    assert record.steps != captured_steps[0]
    assert handle.detail["steps"] == captured_steps[last]
    assert _attempt_segment(record.steps) == str(last)

    # ``steps`` and ``errors`` describe the SAME attempt (both = last).
    assert record.errors == captured_errors[last]
    assert handle.detail["errors"] == captured_errors[last]

    # Scope declaration in code: the cumulative invocation trail is NOT the
    # single-attempt ``steps`` for the deterministic runtime after a replan.
    assert len(record.tool_invocations) >= len(record.steps)


# --------------------------------------------------------------------------- #
# 2. Reverse control: without a replan the write-back is a no-op                #
# --------------------------------------------------------------------------- #
async def test_record_steps_unchanged_without_a_replan(monkeypatch, force_memory_backend):
    captured_steps: dict[int, list[dict[str, Any]]] = {}
    captured_errors: dict[int, list[str]] = {}
    _wrap_real_executor(monkeypatch, captured_steps, captured_errors, fail_first=False)

    handle = await run_task(
        TaskCreate(intent="离线分析一次（不触发重规划）", workflow_type="generic"),
        _ctx(),
        bus=_RecordingBus(),
    )
    record = get_run_store().get(handle.run_id)
    assert record is not None

    # No replan: only attempt 0 ran, so the new write-back never executed and the
    # recorded steps are exactly what the (single) executor returned — i.e. the
    # pre-INC17 behaviour is preserved byte-for-byte.
    assert set(captured_steps) == {0}
    assert record.steps == captured_steps[0]
    assert _attempt_segment(record.steps) == "0"
    assert handle.detail["steps"] == captured_steps[0]
    assert record.errors == []
    assert captured_errors[0] == []
