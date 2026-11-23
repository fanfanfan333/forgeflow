"""FF-R1 B2 (FF-G4) — the per-tool timeout is fail-closed.

Pre-fix holes, both in ``ToolExecutor``:

  * a settings read failure set ``timeout = None`` and the handler ran
    with **no wall clock at all**;
  * the handler ran under ``asyncio.wait_for`` only ``if timeout:`` — a
    timeout value of ``0`` (reachable by direct attribute assignment,
    which bypasses pydantic construction validation) silently disabled
    the cap the same way.

Post-fix contract: for every non-exempt tool the handler always runs
under ``wait_for``; an unreadable or unusable configured value falls
back to ``DEFAULT_TOOL_TIMEOUT_SECONDS`` (120). The timeout outcome
keeps the established record shape (``payload["timeout"] is True``;
status ``error`` for read-only tools, ``outcome_unknown`` for writes).
"""

from __future__ import annotations

import asyncio
import time

import pytest
from pydantic import ValidationError

from forgeflow.config import Settings, get_settings
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


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch):
    async def _fake_sleep(seconds):  # noqa: ANN001, ANN202
        return None

    monkeypatch.setattr(te, "_sleep", _fake_sleep)


def _ctx(**kw) -> ToolCallContext:
    base = dict(
        run_id="run-b2-timeout",
        step_id="run-b2-timeout:0:0",
        tenant_id="t-1",
        user_id="u-1",
        role="admin",
        intent="test",
        attempt=0,
        args={},
    )
    base.update(kw)
    return ToolCallContext(**base)


def _register_slow(calls: dict) -> None:
    async def _slow(args, ctx):  # noqa: ANN001, ANN202
        calls["n"] += 1
        await asyncio.sleep(5)
        return {"ok": True}

    tool_registry.register(
        ToolBinding(
            tool_id="code.lint",
            handler=_slow,
            kind="real",
            provider="test",
            description="hangs far past any test timeout",
        )
    )


def test_default_tool_timeout_is_120() -> None:
    assert te.DEFAULT_TOOL_TIMEOUT_SECONDS == 120


def test_timeout_zero_rejected_at_config_layer() -> None:
    with pytest.raises(ValidationError):
        Settings(tool_timeout_seconds=0)
    with pytest.raises(ValidationError):
        Settings(tool_timeout_seconds=-5)


async def test_settings_read_failure_falls_back_to_default(monkeypatch) -> None:
    """Settings blow up → the default cap still applies (never None)."""
    calls = {"n": 0}
    _register_slow(calls)

    def _boom():
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(te, "get_settings", _boom)
    monkeypatch.setattr(te, "DEFAULT_TOOL_TIMEOUT_SECONDS", 0.2)

    started = time.perf_counter()
    inv = await ToolExecutor().execute("code.lint", ctx=_ctx())
    elapsed = time.perf_counter() - started

    assert inv.payload["timeout"] is True
    assert elapsed < 0.2 * 3 + 1.0  # internal read-only retries included
    assert calls["n"] >= 1


async def test_zero_timeout_value_still_fails_closed(monkeypatch) -> None:
    """A timeout of 0 smuggled past construction must not disable the cap."""
    calls = {"n": 0}
    _register_slow(calls)
    monkeypatch.setattr(get_settings(), "tool_timeout_seconds", 0)
    monkeypatch.setattr(te, "DEFAULT_TOOL_TIMEOUT_SECONDS", 0.2)

    started = time.perf_counter()
    inv = await ToolExecutor().execute("code.lint", ctx=_ctx())
    elapsed = time.perf_counter() - started

    assert inv.payload["timeout"] is True
    assert elapsed < 0.2 * 3 + 1.0
    assert calls["n"] >= 1


async def test_slow_handler_times_out_within_bound(monkeypatch) -> None:
    calls = {"n": 0}
    _register_slow(calls)
    monkeypatch.setattr(get_settings(), "tool_timeout_seconds", 0.2)

    started = time.perf_counter()
    inv = await ToolExecutor().execute("code.lint", ctx=_ctx())
    elapsed = time.perf_counter() - started

    assert inv.status == "error"  # code.lint is read-only
    assert inv.payload["timeout"] is True
    assert inv.payload["timeout_seconds"] == 0.2
    assert elapsed < 0.2 + 1.0 + 0.5  # retries spent 3 x 0.2s caps
    assert calls["n"] == 3  # the executor's internal read-only retries ran


# --------------------------------------------------------------------------- #
# Supplement （大川裁定 on PR #10): runtime-passed 0 and the timeout outcome     #
# --------------------------------------------------------------------------- #
async def test_write_tool_timeout_is_outcome_unknown_and_never_success(monkeypatch) -> None:
    """Ruling 2: assert the *actual* timeout fields, and never success.

    A write-class tool that times out keeps the established shape —
    ``status == "outcome_unknown"`` (its side effects are unknowable)
    with ``payload["timeout"] is True`` — and the outcome must never
    read as a success.
    """
    calls = {"n": 0}

    async def _slow(args, ctx):  # noqa: ANN001, ANN202
        calls["n"] += 1
        await asyncio.sleep(2)
        return {"ok": True}

    tool_registry.register(
        ToolBinding(
            tool_id="ffwrite.fake",
            handler=_slow,
            kind="real",
            provider="test",
            description="write-class fake that hangs",
        )
    )
    monkeypatch.setattr(get_settings(), "tool_timeout_seconds", 0.05)
    started = time.monotonic()
    inv = await ToolExecutor().execute("ffwrite.fake", ctx=_ctx())
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert inv.status == "outcome_unknown"
    assert inv.status != "ok", "a timeout must never be treated as success"
    assert inv.payload["timeout"] is True
    assert inv.payload.get("ok") is not True
    assert calls["n"] == 1, "write tools are never auto-retried after a timeout"


def test_loop_level_policy_timeout_zero_resolves_to_the_default() -> None:
    """Ruling 1: a runtime ``tool_timeout_seconds=0`` must fail closed.

    The react loop's step cap used to be guarded by a bare truthiness
    check, so a policy carrying 0 disabled the loop-level wrapper
    entirely. The resolver maps None → None (the executor's own cap
    governs), a positive value → itself, and 0 / negative / non-finite
    → the fail-closed default.
    """
    from forgeflow.runtime.react_executor import _resolve_loop_tool_timeout
    from forgeflow.runtime.retry_policy import StepPolicy

    assert _resolve_loop_tool_timeout(StepPolicy(tool_timeout_seconds=None)) is None
    assert _resolve_loop_tool_timeout(StepPolicy(tool_timeout_seconds=2.5)) == 2.5
    assert (
        _resolve_loop_tool_timeout(StepPolicy(tool_timeout_seconds=0))
        == te.DEFAULT_TOOL_TIMEOUT_SECONDS
    )
    assert (
        _resolve_loop_tool_timeout(StepPolicy(tool_timeout_seconds=-3))
        == te.DEFAULT_TOOL_TIMEOUT_SECONDS
    )


async def test_loop_level_policy_timeout_zero_still_caps_the_step(monkeypatch) -> None:
    """Behavioral: with policy timeout 0 the step still times out."""
    from forgeflow.runtime.react_executor import ReactExecutor
    from forgeflow.runtime.retry_policy import StepPolicy

    async def _slow(args, ctx):  # noqa: ANN001, ANN202
        await asyncio.sleep(2)
        return {"ok": True}

    tool_registry.register(
        ToolBinding(
            tool_id="research.search",
            handler=_slow,
            kind="real",
            provider="test",
            description="read-only fake that hangs",
        )
    )
    monkeypatch.setattr(te, "DEFAULT_TOOL_TIMEOUT_SECONDS", 0.2)
    policy = StepPolicy(tool_timeout_seconds=0, max_attempts=1)
    started = time.monotonic()
    inv = await ReactExecutor()._execute_tool_with_policy(
        "research.search",
        _ctx(),
        policy_decision="allow",
        approval_id=None,
        blocked_reason=None,
        policy=policy,
        run_attempt=0,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 1.5, "the loop-level cap must cut the step despite policy 0"
    assert inv.status == "error"
    assert inv.status != "ok"
    assert "执行超时" in (inv.error or "")
