"""INC15 ③ — the honest status model (skipped 退役，blocked 上线).

Why this file exists
--------------------
INC12 gave the runtime a five-state contract (``ok`` / ``error`` /
``unavailable`` / ``refused`` / ``skipped``). The ``skipped`` writer status was
dishonest in practice: it was the label the runtime stamped on a step it had
*force-planned* and then could not run because a required input was missing. A
``skipped`` step read like "we chose not to", when the truth was "the plan asked
for it and it had no input".

INC15 retires ``skipped`` as a **writer** status and splits the concept in two:

* ``blocked`` — the step is **needed** but a required input is missing, so the
  handler was never called (``executed=False``, ``invoked=False``,
  ``latency_ms=None``). A blocked step is **not a failure**.
* ``not_applicable`` — the step does not apply to this task at all; it never
  becomes an execution record (an L1-only concept).

Readers stay compatible: :func:`forgeflow.runtime.planning.normalize_status` maps
the legacy ``skipped → blocked`` so a historical run / export renders honestly.

The ``invoked`` flag is added so a consumer can tell "the handler ran" apart from
"a terminal state was recorded": ``ok``/``error`` ⇒ ``invoked=True``;
``unavailable``/``refused``/``blocked`` ⇒ ``invoked=False`` — except a handler
that itself reported ``not_executed`` (it *was* entered, so ``invoked=True`` while
``executed`` stays ``False``).

Every case declares its storage tier via ``force_memory_backend``.
"""

from __future__ import annotations

import pytest

from forgeflow.runtime import tool_registry
from forgeflow.runtime.planning import (
    BLOCKED_STATUS,
    EXECUTED_STATUSES,
    FAILED_STATUSES,
    LEGACY_STATUS_ALIASES,
    NOT_APPLICABLE_STATUS,
    SUCCEEDED_STATUS,
    TERMINAL_STATUSES,
    normalize_status,
)
from forgeflow.runtime.tool_executor import ToolCallContext, ToolExecutor
from forgeflow.runtime.tool_registry import ToolBinding


@pytest.fixture(autouse=True)
def _clean_registry():
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()
    yield
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()


def _ctx(**kw) -> ToolCallContext:
    base = dict(
        run_id="run-inc15",
        step_id="run-inc15:0:0",
        tenant_id="t-inc15",
        user_id="u-inc15",
        role="admin",
        intent="分析文本",
        attempt=0,
        args={},
    )
    base.update(kw)
    return ToolCallContext(**base)


# --------------------------------------------------------------------------- #
# 1. The status vocabulary                                                     #
# --------------------------------------------------------------------------- #
def test_status_vocabulary_is_pinned(force_memory_backend):
    assert SUCCEEDED_STATUS == "ok"
    assert FAILED_STATUSES == frozenset({"error", "unavailable", "refused"})
    assert BLOCKED_STATUS == "blocked"
    assert NOT_APPLICABLE_STATUS == "not_applicable"
    assert EXECUTED_STATUSES == frozenset({"ok", "error"})
    assert TERMINAL_STATUSES == frozenset({"ok", "error", "unavailable", "refused", "blocked"})


def test_legacy_skipped_reader_alias(force_memory_backend):
    assert LEGACY_STATUS_ALIASES == {"skipped": "blocked"}
    assert normalize_status("skipped") == "blocked"


# --------------------------------------------------------------------------- #
# 2. Every writer status maps to a fixed (executed, invoked, latency) triple    #
# --------------------------------------------------------------------------- #
async def test_status_ok(force_memory_backend):
    inv = await ToolExecutor().execute("docs.parse", ctx=_ctx(args={"text": "hello"}))
    assert (inv.status, inv.executed, inv.invoked) == ("ok", True, True)
    assert inv.latency_ms is not None


async def test_status_error(force_memory_backend):
    async def _boom(args, ctx):  # noqa: ANN001
        raise RuntimeError("boom")

    tool_registry.register(ToolBinding("test.boom15", _boom, "real", "test", "raises"))
    inv = await ToolExecutor().execute("test.boom15", ctx=_ctx())
    assert (inv.status, inv.executed, inv.invoked) == ("error", True, True)
    assert inv.latency_ms is not None  # the handler really ran (and raised)


async def test_status_unavailable(force_memory_backend):
    inv = await ToolExecutor().execute("payment.transfer", ctx=_ctx())
    assert (inv.status, inv.executed, inv.invoked) == ("unavailable", False, False)
    assert inv.latency_ms is None


async def test_status_blocked_via_caller_reason(force_memory_backend):
    inv = await ToolExecutor().execute(
        "data.query", ctx=_ctx(), blocked_reason="缺少必需输入：表名(table)"
    )
    assert (inv.status, inv.executed, inv.invoked) == ("blocked", False, False)
    assert inv.latency_ms is None
    assert inv.error and "table" in inv.error


async def test_status_blocked_via_handler_not_executed(force_memory_backend):
    """A handler that reports ``not_executed`` ⇒ blocked, but ``invoked=True``."""

    async def _no_input(args, ctx):  # noqa: ANN001
        return {"ok": False, "not_executed": True, "reason": "没有有效输入"}

    tool_registry.register(
        ToolBinding("test.noinput15", _no_input, "real", "test", "reports not_executed")
    )
    inv = await ToolExecutor().execute("test.noinput15", ctx=_ctx())
    assert inv.status == "blocked"  # NOT the retired "skipped"
    assert inv.executed is False
    # The handler *was* entered (that is how it reported not_executed).
    assert inv.invoked is True
    assert inv.latency_ms is None
    assert inv.summary == "没有有效输入"


async def test_skipped_is_never_written(force_memory_backend):
    """Whatever the input, the executor must never emit the retired status."""
    inv = await ToolExecutor().execute("code.run", ctx=_ctx(args={}))  # no paths
    assert inv.status != "skipped"
    assert inv.status == "blocked"


# --------------------------------------------------------------------------- #
# 3. A blocked step is not a failure                                            #
# --------------------------------------------------------------------------- #
def test_blocked_is_not_in_the_failure_set(force_memory_backend):
    assert "blocked" not in FAILED_STATUSES
    assert "blocked" in TERMINAL_STATUSES


# --------------------------------------------------------------------------- #
# 4. to_dict carries the INC15 fields                                           #
# --------------------------------------------------------------------------- #
async def test_invocation_dict_exposes_invoked_and_latency(force_memory_backend):
    inv = await ToolExecutor().execute("docs.parse", ctx=_ctx(args={"text": "hi"}))
    payload = inv.to_dict()
    assert payload["invoked"] is True
    assert payload["executed"] is True
    assert payload["latency_ms"] == inv.latency_ms
    assert isinstance(payload["latency_ms"], float)
