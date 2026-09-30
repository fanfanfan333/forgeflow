"""INC15 ① — the elapsed-time contract (老实测量，不再伪造 0).

Why this file exists
--------------------
Before INC15 the runtime forced every *unmeasured* latency to ``0`` and truncated
every *measured* one with ``int()``. Two dishonest outcomes followed:

* a step that was **never** measured (blocked / unavailable / refused) still
  carried ``latency_ms == 0`` — a millisecond-precise claim that no measurement
  was ever taken;
* a sub-millisecond handler was truncated to ``0``, so a *real* measurement was
  rendered as "no data".

The fix makes ``latency_ms`` a ``float | None``:

* ``None`` means **never measured** (the handler was not called) — never ``0``;
* a real measurement keeps **sub-millisecond precision** (``round((…)*1000, 3)``,
  no ``int()``) so a sub-ms handler reads e.g. ``0.062``, not ``0``.

The report renders ``未测量`` for a ``None``/``0``/``bool``/non-numeric cell and
``str(value)`` for a real number (INC15 main裁定 §3.1: the *report* says the
literal Chinese ``未测量``).

Every case declares its storage tier via ``force_memory_backend`` (the timer is
pure, but the tier must never be inherited from the environment).
"""

from __future__ import annotations

from forgeflow.runtime import tool_executor as te
from forgeflow.runtime.tool_executor import ToolCallContext, ToolExecutor
from forgeflow.runtime.tool_handlers import _NO_DATA, _format_latency_ms


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
# 1. The timer itself keeps sub-millisecond precision (no int() truncation)     #
# --------------------------------------------------------------------------- #
class _Clock:
    """A monotonic fake ``perf_counter``: returns the queued samples in order."""

    def __init__(self, *samples: float) -> None:
        self._samples = list(samples)
        self._last = samples[-1] if samples else 0.0

    def perf_counter(self) -> float:
        if self._samples:
            self._last = self._samples.pop(0)
        return self._last


def test_elapsed_ms_preserves_sub_millisecond_precision(monkeypatch):
    """A 0.062 ms handler reads ``0.062`` — the truncation to ``0`` is the bug."""
    # started = 1000.0; the in-call sample = 1000.000062 → 0.000062 s → 0.062 ms.
    monkeypatch.setattr(te, "time", _Clock(1000.0, 1000.000062))
    assert te._elapsed_ms(te.time.perf_counter()) == 0.062


def test_elapsed_ms_is_a_float_not_a_truncated_int(monkeypatch):
    monkeypatch.setattr(te, "time", _Clock(1.0, 1.0000005))  # 0.0005 ms
    value = te._elapsed_ms(te.time.perf_counter())
    assert isinstance(value, float)
    assert value == 0.001  # rounded to 3 dp, NOT int()'d to 0


def test_elapsed_ms_rounds_to_three_decimals(monkeypatch):
    monkeypatch.setattr(te, "time", _Clock(0.0, 1.2345678))  # 1234.5678 ms
    assert te._elapsed_ms(te.time.perf_counter()) == 1234.568


# --------------------------------------------------------------------------- #
# 2. A real handler gets a REAL, positive float; an unmeasured step gets None   #
# --------------------------------------------------------------------------- #
async def test_executed_step_records_a_real_positive_latency(force_memory_backend):
    inv = await ToolExecutor().execute(
        "docs.parse", ctx=_ctx(args={"text": "第一段\n\n第二段"})
    )
    assert inv.status == "ok"
    assert inv.executed is True
    assert inv.invoked is True
    assert inv.latency_ms is not None, "已执行却未测量 —— 不可伪造为 0"
    assert isinstance(inv.latency_ms, float)
    assert inv.latency_ms > 0


async def test_blocked_step_is_never_measured(force_memory_backend):
    """A caller-blocked step never calls the handler ⇒ latency is ``None``."""
    inv = await ToolExecutor().execute(
        "data.query",
        ctx=_ctx(args={}),
        blocked_reason="缺少必需输入：表名(table)",
    )
    assert inv.status == "blocked"
    assert inv.executed is False
    assert inv.invoked is False
    assert inv.latency_ms is None, "未测量必须为 None，绝不能是 0"


async def test_unavailable_step_is_never_measured(force_memory_backend):
    inv = await ToolExecutor().execute("payment.transfer", ctx=_ctx())
    assert inv.status == "unavailable"
    assert inv.latency_ms is None


async def test_refused_step_is_never_measured(force_memory_backend, monkeypatch):
    from forgeflow.config import get_settings
    from forgeflow.runtime import tool_registry
    from forgeflow.runtime.tool_registry import ToolBinding

    tool_registry.reset_registry()
    tool_registry.load_default_bindings()

    async def _dev(args, ctx):  # noqa: ANN001
        return {"ok": True, "summary": "dev"}

    tool_registry.register(
        ToolBinding("test.dev15", _dev, "development", "development-stub", "dev stub")
    )
    monkeypatch.setattr(get_settings(), "app_env", "staging")
    try:
        inv = await ToolExecutor().execute("test.dev15", ctx=_ctx())
        assert inv.status == "refused"
        assert inv.latency_ms is None
    finally:
        tool_registry.reset_registry()
        tool_registry.load_default_bindings()


# --------------------------------------------------------------------------- #
# 3. The report's latency cell (main裁定 §3.1: the literal 未测量)              #
# --------------------------------------------------------------------------- #
def test_report_cell_renders_the_chinese_no_data_text(force_memory_backend):
    assert _NO_DATA == "未测量"
    # Everything that is NOT a real positive number renders 未测量.
    for value in (None, 0, 0.0, -1, -0.5, "", "42", [], {}, True, False):
        assert _format_latency_ms(value) == "未测量", repr(value)


def test_report_cell_renders_real_values_including_sub_ms(force_memory_backend):
    assert _format_latency_ms(0.062) == "0.062"  # sub-ms precision preserved
    assert _format_latency_ms(123) == "123"
    assert _format_latency_ms(812.5) == "812.5"


def test_report_cell_never_renders_a_fabricated_zero(force_memory_backend):
    assert _format_latency_ms(0) == "未测量"
    assert _format_latency_ms(None) == "未测量"
