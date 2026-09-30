"""INC26 Q5 — 「声明了数据文件」与「是代码任务」解耦的回归钉子（design §1.4）。

今天真存在的缺陷：`orchestrator.py::_is_code_task` 的第三个条件把
`_resolve_resource_inputs` 的 ``paths`` 也算作代码信号，而 FILE 资源也会往 ``paths``
写自己真实的文件路径 ⇒ **只要声明一个 CSV 文件资源，任务就被误判为代码任务**，数据
分析被错误路由到代码执行面。

本钉子把修好的语义钉死：

* **FILE 资源**（CSV）⇒ `_is_code_task` 为 `False`、`_is_analysis_task` 为 `True`；
* **代码来源**（`ResourceKind.GIT_REPO`，产 `repo_path`）⇒ `_is_code_task` 为 `True`、
  `_is_analysis_task` 为 `False`。

用真实 `ResourceService` 登记（走真实 `resolve_task_inputs`），不 mock —— 只有真实解引用
才能证明 `paths` 与 `repo_path` 的区分是真的。

引文纪律：一律 `file.py::symbol`，不用行号。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forgeflow.resources.service import ResourceService, reset_resource_index
from forgeflow.runtime.orchestrator import (
    RequestContext,
    TaskCreate,
    _is_analysis_task,
    _is_code_task,
)

pytestmark = pytest.mark.asyncio

_CTX = RequestContext(tenant_id="t-inc26-route", user_id="u-inc26", role="manager")


@pytest.fixture(autouse=True)
def _clean_index():
    reset_resource_index()
    try:
        yield
    finally:
        reset_resource_index()


async def _file_task() -> TaskCreate:
    """登记一个真实 CSV 文件资源并声明它（派生 ``paths``，不派生 ``repo_path``）。"""
    service = ResourceService()
    data = b"lead_id,amount\n1,10\n2,20\n3,30\n"
    record = await service.register_file(
        "t-inc26-route", name="leads.csv", data=data, created_by="u-inc26"
    )
    return TaskCreate(
        intent="分析销售线索数据并求和",
        context={"resources": [record.id], "column": "amount"},
    )


async def _code_task(tmp_path: Path) -> TaskCreate:
    """登记一个真实本地代码来源并声明它（派生 ``repo_path``）。"""
    repo = tmp_path / "demo_repo"
    repo.mkdir()
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    service = ResourceService()
    record = await service.register_code(
        "t-inc26-route",
        source={"source_type": "local_path", "identifier": str(repo)},
        created_by="u-inc26",
    )
    return TaskCreate(intent="修复失败的测试", context={"resources": [record.id]})


async def test_file_resource_does_not_flip_the_code_bit(force_memory_backend):
    """今天真存在的缺陷：一个 CSV 文件资源不得把任务判成代码任务。"""
    task = await _file_task()
    assert _is_code_task(task, _CTX) is False, (
        "FILE 资源仍翻转了代码任务位 —— 数据分析会被误路由到代码执行面"
    )
    assert _is_analysis_task(task, _CTX) is True, "FILE 资源应触发分析步注入"


async def test_code_source_still_flips_the_code_bit(force_memory_backend, tmp_path):
    """代码来源（repo_path）必须仍然被判为代码任务（收紧不得误伤）。"""
    task = await _code_task(tmp_path)
    assert _is_code_task(task, _CTX) is True, "代码来源未翻转代码位 —— 代码面被误伤"
    assert _is_analysis_task(task, _CTX) is False, "代码任务不得同时注入分析步"


async def test_plain_task_is_neither(force_memory_backend):
    """没有任何数据/代码输入的普通任务：两个判定都为 False（不注入任何步）。"""
    task = TaskCreate(intent="写一份周报", context={})
    assert _is_code_task(task, _CTX) is False
    assert _is_analysis_task(task, _CTX) is False
