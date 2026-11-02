"""Per-tool timeout + backoff retry (LangGraph TimeoutPolicy/RetryPolicy pattern).

A hung tool used to stall the whole run until the 600s run budget fired;
transient failures got one immediate retry. Now every non-exempt tool
runs under a wall clock, and read-only transient failures retry twice
with exponential backoff + jitter.
"""

from __future__ import annotations

import asyncio

import pytest

from forgeflow.config import get_settings
from forgeflow.runtime import tool_executor as te
from forgeflow.runtime import tool_registry
from forgeflow.runtime.tool_executor import ToolCallContext, ToolExecutor
from forgeflow.runtime.tool_registry import ToolBinding

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clean_registry():
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
        intent="test",
        attempt=0,
        args={},
    )
    base.update(kw)
    return ToolCallContext(**base)


async def test_read_only_timeout_retries_then_reports_error(monkeypatch):
    calls = {"n": 0}
    delays = []

    async def _slow(args, ctx):
        calls["n"] += 1
        await asyncio.sleep(0.5)
        return {"ok": True}

    async def _fake_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(te, "_sleep", _fake_sleep)
    monkeypatch.setattr(get_settings(), "tool_timeout_seconds", 0.05)
    tool_registry.register(
        ToolBinding(
            tool_id="research.fake_slow",
            handler=_slow,
            kind="real",
            provider="test",
            description="hangs past the test timeout",
        )
    )
    inv = await ToolExecutor().execute("research.fake_slow", ctx=_ctx())
    assert inv.status == "error"
    assert inv.payload["timeout"] is True
    assert calls["n"] == 3  # initial + 2 transient retries
    assert len(delays) == 2
    assert delays[1] > delays[0]  # exponential backoff grows


async def test_write_tool_timeout_is_outcome_unknown_without_retry(monkeypatch):
    calls = {"n": 0}

    async def _slow(args, ctx):
        calls["n"] += 1
        await asyncio.sleep(0.5)
        return {"ok": True}

    monkeypatch.setattr(get_settings(), "tool_timeout_seconds", 0.05)
    tool_registry.register(
        ToolBinding(
            tool_id="mywrite.fake",
            handler=_slow,
            kind="real",
            provider="test",
            description="hangs past the test timeout",
        )
    )
    inv = await ToolExecutor().execute("mywrite.fake", ctx=_ctx())
    assert inv.status == "outcome_unknown"
    assert inv.executed is False
    assert calls["n"] == 1  # write tools are never auto-retried


async def test_transient_failure_retries_with_backoff_then_succeeds(monkeypatch):
    calls = {"n": 0}
    delays = []

    async def _flaky(args, ctx):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("connection reset by peer")
        return {"ok": True, "value": 1}

    async def _fake_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(te, "_sleep", _fake_sleep)
    tool_registry.register(
        ToolBinding(
            tool_id="research.fake_flaky",
            handler=_flaky,
            kind="real",
            provider="test",
            description="fails transiently twice",
        )
    )
    inv = await ToolExecutor().execute("research.fake_flaky", ctx=_ctx())
    assert inv.status == "ok"
    assert inv.payload["value"] == 1
    assert calls["n"] == 3
    assert len(delays) == 2


async def test_non_transient_failure_is_not_retried():
    calls = {"n": 0}

    async def _bad(args, ctx):
        calls["n"] += 1
        return {"ok": False, "error": "invalid arguments, not transient"}

    tool_registry.register(
        ToolBinding(
            tool_id="research.fake_bad",
            handler=_bad,
            kind="real",
            provider="test",
            description="deterministic failure",
        )
    )
    inv = await ToolExecutor().execute("research.fake_bad", ctx=_ctx())
    assert inv.status == "error"
    assert calls["n"] == 1


async def test_chinese_timeout_token_is_transient(monkeypatch):
    calls = {"n": 0}
    delays = []

    async def _flaky(args, ctx):
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("上游服务超时，请稍后重试")
        return {"ok": True, "value": 1}

    async def _fake_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(te, "_sleep", _fake_sleep)
    tool_registry.register(
        ToolBinding(
            tool_id="research.fake_chinese_timeout",
            handler=_flaky,
            kind="real",
            provider="test",
            description="fails with a Chinese timeout message",
        )
    )
    inv = await ToolExecutor().execute("research.fake_chinese_timeout", ctx=_ctx())
    assert inv.status == "ok"
    assert inv.payload["value"] == 1
    assert calls["n"] == 2  # transient token "超时" triggers one retry
    assert len(delays) == 1


def test_exempt_tools_cover_own_ceiling_tools():
    assert {"code.execute", "agent.delegate", "report.render"} <= te._TIMEOUT_EXEMPT_TOOLS
