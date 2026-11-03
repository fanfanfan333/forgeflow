"""Sync parse must not defeat the per-tool timeout (d15f911 round).

``document.inspect`` took 29.2s under a 1s per-tool timeout and still
returned success: its synchronous python-docx parse ran on the
event-loop thread, so ``asyncio.wait_for`` never got a chance to fire.
Parse calls now run in ``asyncio.to_thread``; this pins the timeout
firing against a synchronously blocking parse, and guards the normal
parse path against the thread move.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from docx import Document

from forgeflow.config import get_settings
from forgeflow.runtime import tool_executor as te
from forgeflow.runtime import tool_registry
from forgeflow.runtime.tool_executor import ToolCallContext, ToolExecutor

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


def _docx_file(tmp_path) -> str:
    doc = Document()
    doc.add_paragraph("hello")
    path = tmp_path / "sample.docx"
    doc.save(str(path))
    return str(path)


async def test_sync_parse_blocking_loop_still_times_out(monkeypatch, tmp_path):
    calls = {"n": 0}

    def _slow_parse(data):
        calls["n"] += 1
        time.sleep(0.6)  # synchronous block: never yields to the loop
        return SimpleNamespace(
            paragraphs=1, headings=[], tables=0, words=1,
            to_dict=lambda limit=3: {},
        )

    async def _fake_sleep(seconds):
        return None

    monkeypatch.setattr(te, "_sleep", _fake_sleep)
    monkeypatch.setattr(get_settings(), "tool_timeout_seconds", 0.05)
    monkeypatch.setattr("forgeflow.documents.inspect_docx", _slow_parse)

    started = time.perf_counter()
    inv = await ToolExecutor().execute(
        "document.inspect",
        ctx=_ctx(args={"document_paths": [_docx_file(tmp_path)]}),
    )
    elapsed = time.perf_counter() - started

    assert inv.status == "error"
    assert inv.payload["timeout"] is True
    assert calls["n"] == 3  # initial + 2 transient retries for a read tool
    assert elapsed < 0.6  # the run was NOT stalled by the blocking parse


async def test_document_inspect_still_parses_off_thread(tmp_path):
    # Normal path guard: to_thread must not change a real parse result.
    inv = await ToolExecutor().execute(
        "document.inspect",
        ctx=_ctx(args={"document_paths": [_docx_file(tmp_path)]}),
    )
    assert inv.status == "ok"
    assert inv.payload["ok"] is True
    assert inv.payload["format"] == "docx"
