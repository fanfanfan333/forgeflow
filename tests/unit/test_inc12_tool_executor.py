"""INC12 A1 — the ToolExecutor status contract, pinned row by row.

This suite is the *reason* INC12 exists: before it, both runtime executors
recorded every planned step ``status="ok"`` without ever calling a tool. These
tests pin the honest contract of :class:`forgeflow.runtime.tool_executor.ToolExecutor`:

  * every status (``ok`` / ``error`` / ``unavailable`` / ``refused`` / ``blocked``)
    maps to a fixed ``executed`` boolean and a stated reason. INC15 retires the
    old ``skipped`` writer status in favour of ``blocked`` ("needed but missing
    input"); a reader still accepts the legacy value via
    :func:`forgeflow.runtime.planning.normalize_status` (``skipped → blocked``);
  * a tool with no implementation (``UNBOUND_TOOLS``) is always ``unavailable``;
  * a ``development`` binding is ``refused`` outside ``dev`` and the handler is
    **never** called;
  * ``arguments_hash`` is stable for equal args and differs for different args;
  * ``payload`` is bounded and explicitly flagged when truncated.
"""

from __future__ import annotations

import pytest

from forgeflow.config import get_settings
from forgeflow.runtime import tool_registry
from forgeflow.runtime.tool_executor import (
    MAX_PAYLOAD_CHARS,
    ToolCallContext,
    ToolExecutor,
)
from forgeflow.runtime.tool_registry import ToolBinding

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clean_registry():
    """Every test starts from the default bindings and a clean env gate."""
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()
    yield
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()


def _ctx(**kw) -> ToolCallContext:
    base = dict(
        run_id="run-1",
        step_id="run-1:0:0",
        tenant_id="t-1",
        user_id="u-1",
        role="admin",
        intent="分析数据并生成报告",
        attempt=0,
        args={},
    )
    base.update(kw)
    return ToolCallContext(**base)


# --------------------------------------------------------------------------- #
# 1. The five-status contract                                                  #
# --------------------------------------------------------------------------- #
async def test_status_ok_when_handler_really_runs():
    inv = await ToolExecutor().execute(
        "docs.parse", ctx=_ctx(args={"text": "第一段\n\n第二段"})
    )
    assert inv.status == "ok"
    assert inv.executed is True
    assert inv.invoked is True
    assert inv.error is None
    assert inv.result_ref  # evidence left behind
    assert inv.payload["section_count"] == 2
    # INC15 — a real handler is metered; the value is a real float, never 0.
    assert inv.latency_ms is not None and inv.latency_ms > 0


async def test_status_blocked_when_no_valid_input():
    """A handler with no usable input is ``blocked`` — NOT a fake ok.

    INC15 — this was ``skipped`` before; the honest label is ``blocked`` (needed
    but missing input). It is **not** a failure, so ``error`` stays ``None``, and
    it was never measured, so ``latency_ms`` is ``None`` (never ``0``).
    """
    # Empty intent too, so ``docs.parse`` genuinely has no text to parse.
    inv = await ToolExecutor().execute("docs.parse", ctx=_ctx(args={}, intent=""))
    assert inv.status == "blocked"
    assert inv.executed is False
    assert inv.error is None  # blocked is not a failure
    assert inv.latency_ms is None  # never measured (the handler was entered but
    # reported not_executed, so it was not timed either)


async def test_status_error_when_handler_raises():
    async def _boom(args, ctx):  # noqa: ANN001
        raise RuntimeError("handler exploded")

    tool_registry.register(
        ToolBinding("test.boom", _boom, "real", "test", "always raises")
    )
    inv = await ToolExecutor().execute("test.boom", ctx=_ctx())
    assert inv.status == "error"
    assert inv.executed is True
    assert "handler exploded" in (inv.error or "")


async def test_status_error_on_business_failure():
    async def _fail(args, ctx):  # noqa: ANN001
        return {"ok": False, "error": "下游业务失败"}

    tool_registry.register(
        ToolBinding("test.bizfail", _fail, "real", "test", "returns ok=False")
    )
    inv = await ToolExecutor().execute("test.bizfail", ctx=_ctx())
    assert inv.status == "error"
    assert inv.executed is True
    assert "下游业务失败" in (inv.error or "")


async def test_status_unavailable_for_unbound_tool():
    inv = await ToolExecutor().execute("payment.transfer", ctx=_ctx())
    assert inv.status == "unavailable"
    assert inv.executed is False
    assert "未绑定任何实现" in (inv.error or "")


# --------------------------------------------------------------------------- #
# 2. The core counter-example (this is why INC12 exists)                       #
# --------------------------------------------------------------------------- #
async def test_not_executed_handler_is_never_ok():
    """A step whose handler did not run must never be recorded ``ok``."""
    inv = await ToolExecutor().execute(
        "code.run", ctx=_ctx(args={})  # no paths / repo_path
    )
    assert inv.status != "ok"
    assert inv.executed is False
    assert inv.status == "blocked"  # INC15: blocked, not the retired "skipped"


# --------------------------------------------------------------------------- #
# 3. UNBOUND_TOOLS are always unavailable                                      #
# --------------------------------------------------------------------------- #
async def test_all_unbound_tools_are_unavailable():
    assert tool_registry.UNBOUND_TOOLS  # non-empty by construction
    for tool in sorted(tool_registry.UNBOUND_TOOLS):
        assert tool_registry.resolve(tool) is None
        inv = await ToolExecutor().execute(tool, ctx=_ctx())
        assert inv.status == "unavailable", tool
        assert inv.executed is False
        assert inv.payload is None


# --------------------------------------------------------------------------- #
# 4. Environment gate refuses development bindings outside dev                 #
# --------------------------------------------------------------------------- #
async def test_development_binding_refused_outside_dev_without_calling_handler(monkeypatch):
    calls = {"n": 0}

    async def _spy(args, ctx):  # noqa: ANN001
        calls["n"] += 1
        return {"ok": True, "summary": "should never run"}

    tool_registry.register(
        ToolBinding("test.devtool", _spy, "development", "development-stub", "spy")
    )

    # Non-dev: refused, and the handler is never invoked.
    monkeypatch.setattr(get_settings(), "app_env", "staging")
    inv = await ToolExecutor().execute("test.devtool", ctx=_ctx())
    assert inv.status == "refused"
    assert inv.executed is False
    assert calls["n"] == 0
    assert "禁止使用开发态工具" in (inv.error or "")

    # Control: dev allows it and the handler runs.
    monkeypatch.setattr(get_settings(), "app_env", "dev")
    inv2 = await ToolExecutor().execute("test.devtool", ctx=_ctx())
    assert inv2.status == "ok"
    assert calls["n"] == 1


async def test_real_binding_is_not_environment_gated(monkeypatch):
    monkeypatch.setattr(get_settings(), "app_env", "prod")
    inv = await ToolExecutor().execute(
        "docs.parse", ctx=_ctx(args={"text": "hello world"})
    )
    assert inv.status == "ok"  # kind="real" is never refused


# --------------------------------------------------------------------------- #
# 5. arguments_hash stability                                                  #
# --------------------------------------------------------------------------- #
async def test_arguments_hash_stable_and_discriminating():
    executor = ToolExecutor()
    ctx_a = _ctx(args={"text": "same input"})
    inv_1 = await executor.execute("docs.parse", ctx=ctx_a)
    inv_2 = await executor.execute("docs.parse", ctx=_ctx(args={"text": "same input"}))
    inv_3 = await executor.execute("docs.parse", ctx=_ctx(args={"text": "different input"}))
    assert inv_1.arguments_hash == inv_2.arguments_hash
    assert inv_1.arguments_hash != inv_3.arguments_hash


# --------------------------------------------------------------------------- #
# 6. Bounded payload                                                           #
# --------------------------------------------------------------------------- #
async def test_payload_is_bounded_and_flagged():
    async def _huge(args, ctx):  # noqa: ANN001
        return {"ok": True, "blob": "x" * (MAX_PAYLOAD_CHARS * 3)}

    tool_registry.register(
        ToolBinding("test.huge", _huge, "real", "test", "returns a huge payload")
    )
    inv = await ToolExecutor().execute("test.huge", ctx=_ctx())
    assert inv.status == "ok"
    assert isinstance(inv.payload, dict)
    assert inv.payload.get("truncated") is True
    assert inv.payload.get("original_length", 0) > MAX_PAYLOAD_CHARS
    # The stored preview itself is bounded.
    assert len(inv.payload.get("preview", "")) <= MAX_PAYLOAD_CHARS


# --------------------------------------------------------------------------- #
# 7. External output is guarded                                                #
# --------------------------------------------------------------------------- #
async def test_external_tool_output_is_sanitised():
    inv = await ToolExecutor().execute(
        "research.search", ctx=_ctx(args={"query": "Stripe funding"})
    )
    assert inv.status == "ok"
    # External (networked) results pass through the injection guard, which wraps
    # them in the UNTRUSTED_TOOL_OUTPUT envelope.
    assert isinstance(inv.payload, str)
    assert "UNTRUSTED_TOOL_OUTPUT" in inv.payload
    assert inv.development_stub is True  # no Tavily configured in tests
