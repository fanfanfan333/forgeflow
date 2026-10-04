"""GAPFIX-INC47 (F-127) — resource-type-derived tool narrowing.

When a run carries a **declared data file** and no real ``table``, the model is
offered ``analysis.profile`` (moved first) instead of ``data.query``. The signal
is the platform's own ``_is_analysis_task`` — a dereferenced resource yields real
data input — never an intent keyword.

Note the honest boundary: a blob path is content-addressed (no suffix), so a
filename test would be vacuous; the signal is the declared resource, and
``data.query`` (which needs a ``table``) could not have succeeded on such a run
anyway. These tests are additive (red line 1).
"""

from __future__ import annotations

import pytest

from forgeflow.repositories.memory.resource_repo import clear_resource_store
from forgeflow.resources.service import ResourceService, reset_resource_index
from forgeflow.runtime.orchestrator import RequestContext, TaskCreate
from forgeflow.runtime.react_executor import ReactExecutor

TENANT = "t-gapfix2-f127"
BASE_TOOLS = ["research.search", "data.query", "analysis.profile", "code.run"]
CSV = b"region,revenue\nNorth,120000\nSouth,88000\n"


def _ctx() -> RequestContext:
    return RequestContext(tenant_id=TENANT, user_id="u-gapfix2", role="manager")


@pytest.fixture
def memory_resources(force_memory_backend, tmp_path, monkeypatch):
    from forgeflow.config import get_settings

    monkeypatch.setattr(get_settings(), "resource_store_root", str(tmp_path / "blobs"))
    reset_resource_index()
    clear_resource_store()
    yield
    reset_resource_index()
    clear_resource_store()


def _narrow(task):
    return ReactExecutor._narrow_tools_for_declared_data(task, _ctx(), list(BASE_TOOLS))


async def test_data_file_resource_narrows_data_query(memory_resources):
    record = await ResourceService().register_file(TENANT, name="sales.csv", data=CSV)
    task = TaskCreate(
        intent="分析这份销售数据",
        workflow_type="generic",
        context={"resources": [record.id]},
    )
    tools = _narrow(task)
    assert "data.query" not in tools
    assert tools[0] == "analysis.profile"  # promoted to the front


async def test_text_resource_keeps_data_query(memory_resources):
    # A registered .py is a text/code file — not a data file ⇒ no narrowing.
    record = await ResourceService().register_file(TENANT, name="app.py", data=b"x = 1\n")
    task = TaskCreate(
        intent="检查这段代码",
        workflow_type="generic",
        context={"resources": [record.id]},
    )
    assert _narrow(task) == BASE_TOOLS


def test_explicit_table_keeps_data_query():
    task = TaskCreate(
        intent="查询订单",
        workflow_type="generic",
        context={"table": "orders"},
    )
    assert _narrow(task) == BASE_TOOLS


def test_no_resource_keeps_data_query():
    task = TaskCreate(intent="随便聊聊", workflow_type="generic", context={})
    assert _narrow(task) == BASE_TOOLS


def test_narrowing_is_idempotent_when_data_query_absent():
    task = TaskCreate(intent="随便聊聊", workflow_type="generic", context={})
    tools = ["research.search", "analysis.profile"]
    assert ReactExecutor._narrow_tools_for_declared_data(task, _ctx(), tools) == tools
