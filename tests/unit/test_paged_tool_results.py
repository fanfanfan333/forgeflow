"""Paged tool results (Phase 1).

An oversized tool result used to become a preview-only envelope and the
rest was lost to the model. Now the executor stores the full canonical
result in pages and the envelope carries a page handle; ``tool_result.page``
fetches page N without re-running the producing tool. Code-plane payloads
keep their structural trim and are never paged.
"""

from __future__ import annotations

import math

import pytest

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
    te._PAGE_STORE.clear()


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


_SCOPE = {"tenant_id": "t-1", "user_id": "u-1", "run_id": "run-1"}


def _big_value(chars: int = 9000) -> dict:
    return {"text": "x" * chars}


def test_bound_payload_pages_oversized_result():
    value = _big_value()
    bounded, truncated = te._bound_payload(value, tool="textfile.inspect", **_SCOPE)
    assert truncated is True
    # Preview envelope keys stay (backward compatible)...
    assert bounded["truncated"] is True
    assert bounded["original_length"] == len(te._canonical(value))
    assert bounded["preview"] == te._canonical(value)[: te.PAGE_CHARS]
    # ...plus the page handle.
    assert bounded["paged"] is True
    assert bounded["page"] == 0
    assert bounded["pages"] == math.ceil(len(te._canonical(value)) / te.PAGE_CHARS)
    assert bounded["page_size"] == te.PAGE_CHARS
    ref = bounded["page_ref"]
    pages = [te.fetch_page(ref, i, **_SCOPE)["content"] for i in range(bounded["pages"])]
    assert "".join(pages) == te._canonical(value)
    assert te.fetch_page(ref, 0, **_SCOPE)["content"] == bounded["preview"]


def test_bound_payload_leaves_small_and_codeplane_payloads_alone():
    small = {"ok": True, "text": "hi"}
    bounded, truncated = te._bound_payload(small, tool="textfile.inspect")
    assert truncated is False
    assert bounded == small
    codeplane = {"codeplane": {"diff": "d" * 5000, "timeline": [], "tests": {}}}
    bounded, _ = te._bound_payload(codeplane, tool="code.execute")
    assert "paged" not in bounded  # structural trim, never paged
    assert bounded["codeplane"]["diff_truncated"] is True


def test_fetch_page_failures_are_honest():
    assert te.fetch_page("no-such-ref", 0, **_SCOPE)["ok"] is False
    assert te.fetch_page("no-such-ref", 0)["ok"] is False  # missing scope is refused
    ref, _ = te.store_paged_result("abc", **_SCOPE)
    assert te.fetch_page(ref, 1, **_SCOPE)["ok"] is False
    assert te.fetch_page(ref, -1, **_SCOPE)["ok"] is False


def test_page_store_rejects_cross_tenant_user_and_run():
    ref, _ = te.store_paged_result("tenant-secret", **_SCOPE)
    assert te.fetch_page(ref, 0, **_SCOPE)["content"] == "tenant-secret"
    assert te.fetch_page(ref, 0, tenant_id="t-other", user_id="u-1", run_id="run-1")["ok"] is False
    assert te.fetch_page(ref, 0, tenant_id="t-1", user_id="u-other", run_id="run-1")["ok"] is False
    assert te.fetch_page(ref, 0, tenant_id="t-1", user_id="u-1", run_id="run-other")["ok"] is False
    # Same content under another tenant is a different slot.
    other, _ = te.store_paged_result("tenant-secret", tenant_id="t-other", user_id="u-1", run_id="run-1")
    assert other == ref
    assert te.fetch_page(other, 0, tenant_id="t-other", user_id="u-1", run_id="run-1")["ok"] is True
    assert te.fetch_page(ref, 0, tenant_id="t-1", user_id="u-1", run_id="run-1")["content"] == "tenant-secret"


def test_page_store_evicts_oldest_first(monkeypatch):
    monkeypatch.setattr(te, "_PAGE_STORE_MAX_ENTRIES", 2)
    ref1, _ = te.store_paged_result("a" * 5000, **_SCOPE)
    te.store_paged_result("b" * 5000, **_SCOPE)
    te.store_paged_result("c" * 5000, **_SCOPE)
    assert te.fetch_page(ref1, 0, **_SCOPE)["ok"] is False  # evicted


async def test_executor_pages_then_page_tool_fetches():
    async def _big(args, ctx):
        return _big_value()

    tool_registry.register(
        ToolBinding(tool_id="textfile.fake_big", handler=_big, kind="real", provider="test", description="oversized")
    )
    inv = await ToolExecutor().execute("textfile.fake_big", ctx=_ctx())
    assert inv.payload["paged"] is True
    ref = inv.payload["page_ref"]
    page1 = await ToolExecutor().execute("tool_result.page", ctx=_ctx(args={"page_ref": ref, "page": 1}))
    assert page1.status == "ok"
    assert page1.payload["ok"] is True
    assert page1.payload["page"] == 1
    assert page1.payload["content"] == te.fetch_page(ref, 1, **_SCOPE)["content"]
    missing = await ToolExecutor().execute("tool_result.page", ctx=_ctx(args={"page_ref": "nope", "page": 0}))
    assert missing.payload["ok"] is False


def test_page_tool_is_wired_for_the_model():
    from forgeflow.runtime.delegation import ROLE_TOOLS
    from forgeflow.runtime.gate import PLATFORM_PLAN_TOOLS
    from forgeflow.runtime.llm_planner import known_plan_tools
    from forgeflow.runtime.react_executor import ReactExecutor

    assert "tool_result.page" in PLATFORM_PLAN_TOOLS
    assert "tool_result.page" in known_plan_tools()
    assert all("tool_result.page" in ROLE_TOOLS[role] for role in ("research", "document_review", "code_review"))
    assert "tool_result.page" in tool_registry.load_default_bindings()
    schemas = ReactExecutor._tool_schemas(["tool_result.page"])
    assert schemas[0]["function"]["name"] == "tool_result.page"
    assert set(schemas[0]["function"]["parameters"]["properties"]) == {"page_ref", "page"}
