"""FF-R1 B6-FIX — ToolExecutor must never log a tool's raw exception.

Codex retest of PR #21 found the fake secret from a raising tool
handler appearing once in the logs: the handler-exception path wrote
``str(exc)`` to the log (and into the invocation record) before any
redaction happened. These tests pin sanitize-before-logging: neither
the log records nor the returned invocation may carry credential
material a handler echoed into its exception.
"""

import logging

import pytest

from forgeflow.runtime import tool_registry
from forgeflow.runtime.tool_executor import ToolCallContext, ToolExecutor
from forgeflow.runtime.tool_registry import ToolBinding

FAKE_SECRET = "sk-FAKEB6FIX000111222333"
FAKE_PASSWORD = "hunter2fakeb6fix"


@pytest.fixture(autouse=True)
def _clean_registry():
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()
    yield
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()


@pytest.mark.asyncio
async def test_handler_exception_is_redacted_in_logs_and_record(caplog):
    async def _leaky(args: dict, ctx: object) -> dict:
        raise RuntimeError(f"upstream rejected api_key={FAKE_SECRET} password={FAKE_PASSWORD}")

    tool_registry.register(
        ToolBinding(
            tool_id="b6fix.leaky",
            handler=_leaky,
            kind="real",
            provider="test",
            description="handler that echoes credentials in its exception",
        )
    )
    ctx = ToolCallContext(
        run_id="run-b6fix-1",
        step_id="run-b6fix-1:0",
        tenant_id="tenant-b6fix",
        user_id="u-b6fix",
        role="admin",
        args={},
    )
    with caplog.at_level(logging.DEBUG, logger="forgeflow.runtime.tool_executor"):
        invocation = await ToolExecutor().execute("b6fix.leaky", ctx=ctx)

    assert invocation.status in ("error", "outcome_unknown")
    assert FAKE_SECRET not in caplog.text
    assert FAKE_PASSWORD not in caplog.text
    assert FAKE_SECRET not in (invocation.error or "")
    assert FAKE_PASSWORD not in (invocation.error or "")
    assert FAKE_SECRET not in (invocation.summary or "")
    assert FAKE_PASSWORD not in (invocation.summary or "")
