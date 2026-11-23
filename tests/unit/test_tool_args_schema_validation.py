"""FF-R1 B2 (FF-G10) — tool arguments are JSON-schema validated
fail-closed before the handler runs (R1.1 定案).

Contract (supersedes the original B2 draft where they conflict):

  * validation uses ``jsonschema.Draft202012Validator`` against the
    binding's ``args_schema``, inside ``ToolExecutor.execute``, before
    the handler (and before the approval gate);
  * a failing call does **not** invoke the handler; the invocation is a
    tool result carrying ``TOOL_ARGUMENT_VALIDATION_ERROR`` with the
    per-error ``path`` / ``keyword`` / ``message`` list, fed back to the
    model like any other tool result;
  * only argument errors become results — a broken schema (an internal
    error) still raises;
  * tools whose binding has no ``args_schema`` (still-empty schemas)
    are skipped, exactly as before.
"""

from __future__ import annotations

import pytest

from forgeflow.runtime import tool_registry
from forgeflow.runtime.tool_executor import ToolCallContext, ToolExecutor
from forgeflow.runtime.tool_registry import ToolBinding

pytestmark = pytest.mark.asyncio

_RESEARCH_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string", "minLength": 1}},
    "required": ["query"],
    "additionalProperties": False,
}


@pytest.fixture(autouse=True)
def _clean_registry():
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()
    yield
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()


def _ctx(args, **kw) -> ToolCallContext:
    base = dict(
        run_id="run-b2-args",
        step_id="run-b2-args:0:0",
        tenant_id="t-1",
        user_id="u-1",
        role="admin",
        intent="test",
        attempt=0,
        args=args,
    )
    base.update(kw)
    return ToolCallContext(**base)


def _register_search(calls: dict, *, schema=_RESEARCH_SCHEMA) -> None:
    async def _handler(args, ctx):  # noqa: ANN001, ANN202
        calls["n"] += 1
        calls["args"] = args
        return {"ok": True, "results": []}

    tool_registry.register(
        ToolBinding(
            tool_id="research.search",
            handler=_handler,
            kind="real",
            provider="test",
            description="counting fake",
            args_schema=schema,
        )
    )


async def test_wrong_type_returns_validation_error_and_skips_handler() -> None:
    calls = {"n": 0}
    _register_search(calls)

    inv = await ToolExecutor().execute("research.search", ctx=_ctx({"query": 123}))

    assert calls["n"] == 0, "handler must not run for invalid args"
    assert inv.executed is False
    assert inv.payload["ok"] is False
    assert inv.payload["error_code"] == "TOOL_ARGUMENT_VALIDATION_ERROR"
    errors = inv.payload["validation_errors"]
    assert errors, "validation errors must be attached"
    first = errors[0]
    assert first["path"] == "query"
    assert first["keyword"] == "type"
    assert first["message"]
    assert "TOOL_ARGUMENT_VALIDATION_ERROR" in (inv.error or "")


async def test_missing_required_field_is_a_validation_error() -> None:
    calls = {"n": 0}
    _register_search(calls)

    inv = await ToolExecutor().execute("research.search", ctx=_ctx({}))

    assert calls["n"] == 0
    assert inv.payload["error_code"] == "TOOL_ARGUMENT_VALIDATION_ERROR"
    assert any(e["keyword"] == "required" for e in inv.payload["validation_errors"])


async def test_additional_property_is_a_validation_error() -> None:
    calls = {"n": 0}
    _register_search(calls)

    inv = await ToolExecutor().execute(
        "research.search", ctx=_ctx({"query": "forgeflow", "bogus": 1})
    )

    assert calls["n"] == 0
    assert inv.payload["error_code"] == "TOOL_ARGUMENT_VALIDATION_ERROR"
    assert any(e["keyword"] == "additionalProperties" for e in inv.payload["validation_errors"])


async def test_valid_args_reach_the_handler() -> None:
    calls = {"n": 0}
    _register_search(calls)

    inv = await ToolExecutor().execute("research.search", ctx=_ctx({"query": "forgeflow"}))

    assert calls["n"] == 1
    assert inv.status == "ok"
    assert inv.executed is True


async def test_tool_without_schema_is_not_validated() -> None:
    calls = {"n": 0}

    async def _handler(args, ctx):  # noqa: ANN001, ANN202
        calls["n"] += 1
        return {"ok": True}

    tool_registry.register(
        ToolBinding(
            tool_id="policy.check",
            handler=_handler,
            kind="real",
            provider="test",
            description="no schema — skipped by design",
        )
    )

    inv = await ToolExecutor().execute("policy.check", ctx=_ctx({"anything": 123}))

    assert calls["n"] == 1
    assert inv.status == "ok"


async def test_broken_schema_raises_instead_of_returning_a_result() -> None:
    """Only *argument* errors become results; internal errors raise."""
    from jsonschema.exceptions import SchemaError, UnknownType

    calls = {"n": 0}
    _register_search(calls, schema={"type": "not-a-real-type"})

    with pytest.raises((SchemaError, UnknownType)):
        await ToolExecutor().execute("research.search", ctx=_ctx({"query": "x"}))
    assert calls["n"] == 0


async def test_production_research_search_binding_carries_a_schema() -> None:
    """The shipped binding (not just test fakes) is validation-enabled."""
    binding = tool_registry.resolve("research.search")
    assert binding is not None
    assert binding.args_schema is not None
    assert binding.args_schema.get("additionalProperties") is False
    assert "query" in (binding.args_schema.get("required") or [])
