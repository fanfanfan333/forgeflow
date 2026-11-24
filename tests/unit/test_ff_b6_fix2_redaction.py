"""FF-R1 B6-FIX2 — one sanitizer for every ToolExecutor exit.

Codex's retest of 7acfa3a probed four credential shapes through the
executor and found the URL-userinfo and JSON-field shapes still
leaking — above all on the SUCCESS path, where ``ToolInvocation``
payloads were recorded verbatim. These tests parametrize the shapes
through both the success and the exception path and assert zero
occurrences at all four exits (logs, error, return value, persisted
record), plus that ordinary content survives unsanitized.
"""

import json
import logging

import pytest

from forgeflow.runtime import tool_executor as te
from forgeflow.runtime import tool_registry
from forgeflow.runtime.tool_executor import ToolCallContext, ToolExecutor
from forgeflow.runtime.tool_registry import ToolBinding

MARKERS = {
    "sk_keyvalue": "sk-FAKEFIX2AAAA1111",
    "bearer": "FAKEFIX2BBBB2222",
    "url_userinfo": "FAKEFIX2CCCC3333",
    "json_field": "FAKEFIX2DDDD4444",
}


def _payload_for(shape: str, marker: str):
    """A handler payload embedding ``marker`` in the given shape."""
    if shape == "sk_keyvalue":
        return {"ok": True, "value": 3, "note": f"upstream replied api_key={marker} ok"}
    if shape == "bearer":
        return {"ok": True, "value": 3, "note": f"Authorization: Bearer {marker}"}
    if shape == "url_userinfo":
        return {"ok": True, "value": 3, "endpoint": f"https://user:{marker}@example.com/hook"}
    if shape == "json_field":
        return {
            "ok": True,
            "value": 3,
            "config": {"Password": marker, "nested": {"client_secret": marker}},
            "raw": json.dumps({"access_token": marker, "city": "Shanghai"}),
        }
    raise AssertionError(shape)


def _message_for(shape: str, marker: str) -> str:
    payload = _payload_for(shape, marker)
    return json.dumps(payload, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _clean_registry():
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()
    yield
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()


@pytest.fixture()
def _persisted(monkeypatch):
    records: list[dict] = []

    async def _capture(invocation, ctx):
        records.append(invocation.to_dict())

    monkeypatch.setattr(te, "_persist_best_effort", _capture)
    return records


def _ctx() -> ToolCallContext:
    return ToolCallContext(
        run_id="run-b6fix2",
        step_id="run-b6fix2:0",
        tenant_id="tenant-b6fix2",
        user_id="u-b6fix2",
        role="admin",
        args={},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", list(MARKERS))
async def test_success_path_sanitizes_all_exits(shape, caplog, _persisted):
    marker = MARKERS[shape]

    async def _handler(args: dict, ctx: object) -> dict:
        return _payload_for(shape, marker)

    tool_registry.register(
        ToolBinding(
            tool_id="b6fix2.ok", handler=_handler, kind="real",
            provider="test", description="returns a credential-bearing payload",
        )
    )
    with caplog.at_level(logging.DEBUG, logger="forgeflow.runtime.tool_executor"):
        invocation = await ToolExecutor().execute("b6fix2.ok", ctx=_ctx())

    assert invocation.status == "ok"
    returned = json.dumps(invocation.to_dict(), ensure_ascii=False, default=str)
    assert _persisted, "the invocation must reach the record exit"
    record = json.dumps(_persisted[-1], ensure_ascii=False, default=str)
    assert marker not in caplog.text, "log exit"
    assert marker not in (invocation.error or ""), "error exit"
    assert marker not in returned, "return exit"
    assert marker not in record, "record exit"
    # No over-redaction: ordinary content survives.
    assert invocation.payload["value"] == 3
    if shape == "url_userinfo":
        assert "example.com" in invocation.payload["endpoint"]
    if shape == "json_field":
        assert json.loads(invocation.payload["raw"])["city"] == "Shanghai"


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", list(MARKERS))
async def test_exception_path_sanitizes_all_exits(shape, caplog, _persisted):
    marker = MARKERS[shape]

    async def _handler(args: dict, ctx: object) -> dict:
        raise RuntimeError(_message_for(shape, marker))

    tool_registry.register(
        ToolBinding(
            tool_id="b6fix2.boom", handler=_handler, kind="real",
            provider="test", description="raises with credentials in the message",
        )
    )
    with caplog.at_level(logging.DEBUG, logger="forgeflow.runtime.tool_executor"):
        invocation = await ToolExecutor().execute("b6fix2.boom", ctx=_ctx())

    assert invocation.status in ("error", "outcome_unknown")
    returned = json.dumps(invocation.to_dict(), ensure_ascii=False, default=str)
    assert _persisted, "the invocation must reach the record exit"
    record = json.dumps(_persisted[-1], ensure_ascii=False, default=str)
    assert marker not in caplog.text, "log exit"
    assert marker not in (invocation.error or ""), "error exit"
    assert marker not in (invocation.summary or ""), "error exit (summary)"
    assert marker not in returned, "return exit"
    assert marker not in record, "record exit"
