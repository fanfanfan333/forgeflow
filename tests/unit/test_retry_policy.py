"""Core-loop stability — the unified step policy (retry_policy).

Unit coverage for phase 1:

* policy resolution (defaults + fail-closed task.context overrides);
* error classification (retryable vs non-retryable);
* backoff bounds (exponential, capped, jittered);
* the write-tool retry gate (both sides);
* loop-level wrappers: tool timeout, escaping exceptions, and the
  write gate applied to real handler call counts;
* model-call retry / timeout / cancellation;
* dead-loop tracker accounting, structured observations, truncation.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from forgeflow.runtime import tool_registry
from forgeflow.runtime.react_executor import ReactExecutor
from forgeflow.runtime.retry_policy import (
    DEFAULT_LOOP_TOKEN_BUDGET,
    DEFAULT_MAX_ATTEMPTS,
    TRUNCATION_MARKER,
    LoopGuardTracker,
    StepPolicy,
    backoff_delay,
    classify_error,
    classify_error_text,
    is_write_class_tool,
    resolve_step_policy,
    structured_error_observation,
    truncate_feedback,
    write_retry_allowed,
)
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


def _task(context=None):
    return SimpleNamespace(context=dict(context or {}))


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


def _tiny_policy(**kw) -> StepPolicy:
    base = dict(
        max_attempts=3,
        initial_backoff_seconds=0.001,
        max_backoff_seconds=0.002,
        jitter_seconds=0.0,
        model_timeout_seconds=None,
        tool_timeout_seconds=None,
    )
    base.update(kw)
    return StepPolicy(**base)


# --- resolution --------------------------------------------------------------- #


def test_resolve_defaults():
    policy = resolve_step_policy(_task())
    assert policy.max_attempts == DEFAULT_MAX_ATTEMPTS == 3
    assert policy.loop_token_budget == DEFAULT_LOOP_TOKEN_BUDGET
    assert policy.model_timeout_seconds == 300.0
    assert policy.tool_timeout_seconds is None  # ToolExecutor's own clock governs
    assert policy.dead_loop_identical_limit == 3
    assert policy.dead_loop_error_limit == 5


def test_resolve_valid_overrides():
    policy = resolve_step_policy(
        _task(
            {
                "retry_max_attempts": 2,
                "retry_initial_backoff_seconds": 0.01,
                "tool_step_timeout_seconds": 5,
                "dead_loop_identical_limit": 5,
                "loop_token_budget": 1000,
                "tool_feedback_max_chars": 500,
            }
        )
    )
    assert policy.max_attempts == 2
    assert policy.initial_backoff_seconds == 0.01
    assert policy.tool_timeout_seconds == 5.0
    assert policy.dead_loop_identical_limit == 5
    assert policy.loop_token_budget == 1000
    assert policy.tool_feedback_max_chars == 500


@pytest.mark.parametrize(
    "key,bad",
    [
        ("retry_max_attempts", 0),
        ("retry_max_attempts", True),
        ("retry_max_attempts", "3"),
        ("retry_max_attempts", 99),
        ("model_timeout_seconds", 0),
        ("model_timeout_seconds", -5),
        ("loop_token_budget", 0),
        ("dead_loop_error_limit", 0),
        ("tool_feedback_max_chars", 10),
    ],
)
def test_resolve_invalid_overrides_fall_back(key, bad):
    policy = resolve_step_policy(_task({key: bad}))
    default = resolve_step_policy(_task())
    field = {
        "retry_max_attempts": "max_attempts",
        "model_timeout_seconds": "model_timeout_seconds",
        "loop_token_budget": "loop_token_budget",
        "dead_loop_error_limit": "dead_loop_error_limit",
        "tool_feedback_max_chars": "tool_feedback_max_chars",
    }[key]
    assert getattr(policy, field) == getattr(default, field)


# --- classification ----------------------------------------------------------- #


class _StatusError(Exception):
    def __init__(self, status_code):
        super().__init__(f"http {status_code}")
        self.status_code = status_code


class RateLimitError(Exception):
    pass


@pytest.mark.parametrize(
    "exc,expected",
    [
        (TimeoutError("slow"), ("timeout", True)),
        (ConnectionError("reset"), ("connection", True)),
        (OSError("io"), ("connection", True)),
        (_StatusError(429), ("rate_limit", True)),
        (_StatusError(503), ("server_error", True)),
        (_StatusError(400), ("http_400", False)),
        (_StatusError(403), ("http_403", False)),
        (RateLimitError("rl"), ("RateLimitError", True)),
        (ValueError("connection string invalid"), ("ValueError", False)),
        (PermissionError("denied"), ("PermissionError", False)),
        (TypeError("bad arg"), ("TypeError", False)),
        (RuntimeError("weird state"), ("RuntimeError", False)),
    ],
)
def test_classify_error(exc, expected):
    assert classify_error(exc) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("tool execution timeout after 30s", ("timeout", True)),
        ("HTTP 429 rate limit exceeded", ("rate_limit", True)),
        ("upstream 503", ("server_error", True)),
        ("connection refused", ("connection", True)),
        ("validation failed: amount required", ("tool_error", False)),
        ("", ("tool_error", False)),
    ],
)
def test_classify_error_text(text, expected):
    assert classify_error_text(text) == expected


# --- backoff ------------------------------------------------------------------ #


def test_backoff_is_exponential_and_capped_without_jitter():
    policy = StepPolicy(initial_backoff_seconds=0.5, max_backoff_seconds=8.0, jitter_seconds=0.0)
    assert backoff_delay(policy, 0) == 0.5
    assert backoff_delay(policy, 1) == 1.0
    assert backoff_delay(policy, 2) == 2.0
    assert backoff_delay(policy, 10) == 8.0


def test_backoff_jitter_stays_within_bounds():
    policy = StepPolicy(initial_backoff_seconds=0.5, max_backoff_seconds=8.0, jitter_seconds=0.25)
    for _ in range(50):
        delay = backoff_delay(policy, 1)
        assert 1.0 <= delay <= 1.25


# --- write gate ----------------------------------------------------------------- #


def test_write_classification_is_conservative():
    assert is_write_class_tool("research.search") is False
    assert is_write_class_tool("textfile.inspect") is False
    assert is_write_class_tool("code.execute") is True
    assert is_write_class_tool("textfile.edit") is True
    assert is_write_class_tool("totally.unknown") is True


def test_write_retry_gate_both_sides():
    # Read-only: always eligible.
    assert write_retry_allowed("research.search") is True
    # Write without a key: never.
    assert write_retry_allowed("textfile.edit") is False
    # Write with a key that was never persisted: still never.
    assert write_retry_allowed("textfile.edit", idempotency_key="run:1:c1") is False
    assert (
        write_retry_allowed("textfile.edit", idempotency_key="", idempotency_persisted=True)
        is False
    )
    # Write with a persisted key but NO idempotency declaration from
    # the tool: still never — a handler that ignores the key would
    # re-apply its side effect on every retry.
    assert (
        write_retry_allowed("textfile.edit", idempotency_key="run:1:c1", idempotency_persisted=True)
        is False
    )
    # Persisted key + the tool declares it dedupes by that key: allowed.
    assert (
        write_retry_allowed(
            "textfile.edit",
            idempotency_key="run:1:c1",
            idempotency_persisted=True,
            tool_idempotent=True,
        )
        is True
    )
    # Declared + key, but the key was never persisted: still never.
    assert (
        write_retry_allowed("textfile.edit", idempotency_key="run:1:c1", tool_idempotent=True)
        is False
    )
    # MCP tools: the key is never sent to the server, so no MCP write
    # can dedupe a replay — never eligible, declared or not.
    assert (
        write_retry_allowed(
            "mcp.internal.sample_write",
            idempotency_key="run:1:c1",
            idempotency_persisted=True,
            tool_idempotent=True,
        )
        is False
    )


# --- tracker / observation / truncation ---------------------------------------- #


def test_loop_guard_tracker_streaks():
    tracker = LoopGuardTracker()
    sig_a = ("tool.a", "{}")
    sig_b = ("tool.b", "{}")
    assert tracker.note_executed(sig_a) == 1
    assert tracker.note_executed(sig_a) == 2
    assert tracker.note_executed(sig_b) == 1  # different call resets
    assert tracker.note_outcome("error") == 1
    assert tracker.note_outcome("unavailable") == 2
    assert tracker.note_outcome("ok") == 0  # success resets
    assert tracker.note_outcome("error") == 1
    assert tracker.note_outcome("refused") == 0  # gates are not error streaks


def test_structured_error_observation_shape():
    obs = structured_error_observation(
        "research.search",
        status="error",
        error_kind="timeout",
        message="too slow",
        retryable=True,
        attempts=3,
        timeout=True,
    )
    assert obs == {
        "ok": False,
        "tool": "research.search",
        "status": "error",
        "error": {"kind": "timeout", "message": "too slow", "retryable": True},
        "attempts": 3,
        "timeout": True,
    }


def test_truncate_feedback_marks_and_preserves_short_text():
    short, truncated = truncate_feedback("abc", 100)
    assert (short, truncated) == ("abc", False)
    long_text = "x" * 5000
    cut, truncated = truncate_feedback(long_text, 300)
    assert truncated is True
    # The cap covers the TOTAL length, marker included.
    assert len(cut) <= 300
    assert TRUNCATION_MARKER in cut
    assert "5000" in cut
    # The marker's shown count is the actual number of prefix chars.
    prefix = cut.split("\n" + TRUNCATION_MARKER)[0]
    assert set(prefix) == {"x"}
    assert f"仅显示前 {len(prefix)} 字符" in cut
    assert len(cut) < len(long_text)


@pytest.mark.parametrize("original_length", [16001, 20000, 100000])
def test_truncate_feedback_total_length_never_exceeds_cap(original_length):
    text = "x" * original_length
    cut, truncated = truncate_feedback(text, 16000)
    assert truncated is True
    assert len(cut) <= 16000
    assert TRUNCATION_MARKER in cut
    assert str(original_length) in cut  # marker states the original length


def test_truncate_feedback_small_cap_still_respects_cap():
    cut, truncated = truncate_feedback("y" * 5000, 100)
    assert truncated is True
    assert len(cut) <= 100
    assert TRUNCATION_MARKER in cut
    assert "5000" in cut


# --- tool wrapper: gate applied to real call counts ------------------------------ #


async def test_wrapper_write_tool_not_retried_without_persisted_key():
    calls = {"n": 0}

    async def _fail(args, ctx):
        calls["n"] += 1
        raise ConnectionError("connection reset")

    tool_registry.register(
        ToolBinding(
            tool_id="mock.write", handler=_fail, kind="real", provider="test", description="write"
        )
    )
    inv = await ReactExecutor()._execute_tool_with_policy(
        "mock.write",
        _ctx(),
        policy_decision="allow",
        approval_id=None,
        blocked_reason=None,
        policy=_tiny_policy(),
        run_attempt=0,
    )
    # A write handler that died mid-flight is outcome_unknown (the
    # side effect may have landed), never a confirmed plain error.
    assert inv.status == "outcome_unknown"
    assert calls["n"] == 1  # gate shut: no retry for a write without a persisted key


async def test_wrapper_write_tool_retried_with_persisted_key_and_declaration():
    calls = {"n": 0}

    async def _fail(args, ctx):
        calls["n"] += 1
        raise ConnectionError("connection reset")

    tool_registry.register(
        ToolBinding(
            tool_id="mock.write",
            handler=_fail,
            kind="real",
            provider="test",
            description="write",
            idempotent=True,  # the tool declares it dedupes by the key
        )
    )
    inv = await ReactExecutor()._execute_tool_with_policy(
        "mock.write",
        _ctx(idempotency_key="run-1:0:call-1", idempotency_persisted=True),
        policy_decision="allow",
        approval_id=None,
        blocked_reason=None,
        policy=_tiny_policy(),
        run_attempt=0,
    )
    # Retried up to the policy cap; the final attempt's outcome is
    # still genuinely unknown (the handler kept dying after — per its
    # declaration — safely deduping the replay).
    assert inv.status == "outcome_unknown"
    assert calls["n"] == 3  # policy max_attempts, gate open
    assert inv._loop_attempts == 3


async def test_wrapper_read_only_retries_come_from_executor_once():
    # Read-only probes here register as ``evidence.read`` — a
    # whitelisted read-only id (round 2 removed prefix-based
    # classification; synthetic ``research.*`` ids are write-class).
    calls = {"n": 0}

    async def _fail(args, ctx):
        calls["n"] += 1
        raise ConnectionError("connection reset")

    tool_registry.register(
        ToolBinding(
            tool_id="evidence.read",
            handler=_fail,
            kind="real",
            provider="test",
            description="read",
        )
    )
    inv = await ReactExecutor()._execute_tool_with_policy(
        "evidence.read",
        _ctx(),
        policy_decision="allow",
        approval_id=None,
        blocked_reason=None,
        policy=_tiny_policy(),
        run_attempt=0,
    )
    assert inv.status == "error"
    # ToolExecutor's own transient retry (3 attempts total) — the loop
    # wrapper must not stack a second policy on top.
    assert calls["n"] == 3
    assert inv._loop_attempts == 3


async def test_wrapper_loop_level_timeout_retries_read_only_tool():
    calls = {"n": 0}

    async def _slow(args, ctx):
        calls["n"] += 1
        await asyncio.sleep(0.5)
        return {"ok": True}

    tool_registry.register(
        ToolBinding(
            tool_id="evidence.read",
            handler=_slow,
            kind="real",
            provider="test",
            description="slow read",
        )
    )
    inv = await ReactExecutor()._execute_tool_with_policy(
        "evidence.read",
        _ctx(),
        policy_decision="allow",
        approval_id=None,
        blocked_reason=None,
        policy=_tiny_policy(tool_timeout_seconds=0.05),
        run_attempt=0,
    )
    assert inv.status == "error"
    assert inv.payload["timeout"] is True
    assert inv.payload["error"]["kind"] == "timeout"
    assert calls["n"] == 3


async def test_wrapper_loop_level_timeout_write_tool_is_outcome_unknown_once():
    calls = {"n": 0}

    async def _slow(args, ctx):
        calls["n"] += 1
        await asyncio.sleep(0.5)
        return {"ok": True}

    tool_registry.register(
        ToolBinding(
            tool_id="mock.slowrite",
            handler=_slow,
            kind="real",
            provider="test",
            description="slow write",
        )
    )
    inv = await ReactExecutor()._execute_tool_with_policy(
        "mock.slowrite",
        _ctx(),
        policy_decision="allow",
        approval_id=None,
        blocked_reason=None,
        policy=_tiny_policy(tool_timeout_seconds=0.05),
        run_attempt=0,
    )
    assert inv.status == "outcome_unknown"
    assert calls["n"] == 1  # a timed-out write is never replayed


async def test_wrapper_contains_exceptions_escaping_execute(monkeypatch):
    calls = {"n": 0}

    async def _boom(self, tool, *, ctx, policy_decision="x", approval_id=None, blocked_reason=None):
        calls["n"] += 1
        raise RuntimeError("framework exploded")

    monkeypatch.setattr(ToolExecutor, "execute", _boom)
    inv = await ReactExecutor()._execute_tool_with_policy(
        "research.search",
        _ctx(),
        policy_decision="allow",
        approval_id=None,
        blocked_reason=None,
        policy=_tiny_policy(),
        run_attempt=0,
    )
    assert inv.status == "error"
    assert inv.provider == "step_policy"
    assert inv.payload["error"]["kind"] == "RuntimeError"
    assert "framework exploded" in inv.payload["error"]["message"]
    assert calls["n"] == 1  # non-retryable: no retry


async def test_wrapper_retries_retryable_framework_exceptions(monkeypatch):
    calls = {"n": 0}

    async def _boom(self, tool, *, ctx, policy_decision="x", approval_id=None, blocked_reason=None):
        calls["n"] += 1
        raise ConnectionError("gateway down")

    monkeypatch.setattr(ToolExecutor, "execute", _boom)
    inv = await ReactExecutor()._execute_tool_with_policy(
        "research.search",
        _ctx(),
        policy_decision="allow",
        approval_id=None,
        blocked_reason=None,
        policy=_tiny_policy(),
        run_attempt=0,
    )
    assert inv.status == "error"
    assert calls["n"] == 3


# --- model wrapper --------------------------------------------------------------- #


class _FlakyBound:
    def __init__(self, failures: int, exc_factory=ConnectionError):
        self.calls = 0
        self._failures = failures
        self._exc_factory = exc_factory

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        if self.calls <= self._failures:
            raise self._exc_factory("provider down")
        return AIMessage(content="done")


async def test_model_wrapper_retries_transient_then_succeeds():
    bound = _FlakyBound(failures=2)
    result = await ReactExecutor()._ainvoke_with_policy(
        bound, None, [], run_id="r", turn=0, stream=None, policy=_tiny_policy()
    )
    assert result.content == "done"
    assert bound.calls == 3


async def test_model_wrapper_non_retryable_fails_immediately():
    bound = _FlakyBound(failures=99, exc_factory=ValueError)
    with pytest.raises(ValueError):
        await ReactExecutor()._ainvoke_with_policy(
            bound, None, [], run_id="r", turn=0, stream=None, policy=_tiny_policy()
        )
    assert bound.calls == 1


async def test_model_wrapper_timeout_is_retryable_and_bounded():
    class _SlowBound:
        def __init__(self):
            self.calls = 0

        async def ainvoke(self, messages, **kwargs):
            self.calls += 1
            await asyncio.sleep(1)
            return AIMessage(content="late")

    bound = _SlowBound()
    with pytest.raises(TimeoutError):
        await ReactExecutor()._ainvoke_with_policy(
            bound,
            None,
            [],
            run_id="r",
            turn=0,
            stream=None,
            policy=_tiny_policy(max_attempts=2, model_timeout_seconds=0.05),
        )
    assert bound.calls == 2


async def test_model_wrapper_cancellation_propagates():
    class _SlowBound:
        async def ainvoke(self, messages, **kwargs):
            await asyncio.sleep(10)
            return AIMessage(content="never")

    task = asyncio.create_task(
        ReactExecutor()._ainvoke_with_policy(
            _SlowBound(),
            None,
            [],
            run_id="r",
            turn=0,
            stream=None,
            policy=_tiny_policy(model_timeout_seconds=None),
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
