"""INC55 缺陷① —— ReAct 提示必须披露「本次任务已附加的资源」。

现场（真实落库 run ``665585a8-…``）：用户上传 ``新建 文本文档 (2).txt``
（资源 id ``res_b8d6ef58-…``）后提问「查看该文件是否正确，不正确进行修改」，
``declared_inputs.resources`` 确实到达后端、``textfile.inspect`` 也在允许集内，
但模型 ``iterations=1`` / ``tool_invocations=[]``，最终向用户索要文件内容。

根因：``ReactExecutor._messages()`` 构造的 HumanMessage 只有「用户任务 / 工作流类型 /
可用技能」三行，**从未**告诉模型「本次附加了文件」。平台只在
``_effective_args()`` 里把 ``paths`` / ``table`` / ``repo_path`` 当平台自有入参注入到
**工具调用参数**，却从没在提示里声明资源存在 —— 模型不知道文件存在，只能向用户索取。

本文件钉住修复：资源**声明**时披露真实注册名 + 读取工具 + 硬约束；**未声明**时提示词
与改动前**逐字节相同**；并含**反事实**测试证明断言有载荷（不是恒真）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from forgeflow.repositories.memory.resource_repo import clear_resource_store
from forgeflow.resources.service import ResourceService, reset_resource_index
from forgeflow.runtime import orchestrator as orch
from forgeflow.runtime.orchestrator import RequestContext, TaskCreate
from forgeflow.runtime.react_executor import _RESOURCE_BLOCK_HEADER, ReactExecutor

pytestmark = pytest.mark.asyncio

#: 改款前的 HumanMessage 组装式（逐字）。**这就是字节级回归的期望值**。
_PRE_CHANGE_HUMAN = "用户任务：检查文件是否正确\n工作流类型：generic\n可用技能：无"


def _ctx() -> RequestContext:
    return RequestContext(tenant_id="t-inc55", user_id="u-inc55", role="manager")


def _executor() -> ReactExecutor:
    return ReactExecutor()


def _human(task: TaskCreate) -> str:
    msgs = _executor()._messages(task, _ctx(), ["textfile.inspect"])
    # [0] 是 SystemMessage；[1] 是 HumanMessage。
    return msgs[1].content


@pytest.fixture
def memory_resources(force_memory_backend):
    """干净的内存资源仓库 + 解引用索引（一个测试一份）。"""
    reset_resource_index()
    clear_resource_store()
    yield
    reset_resource_index()
    clear_resource_store()


@pytest.fixture
def blob_root(tmp_path, monkeypatch):
    """把默认 ``FileBlobStore`` 根指向仓库外临时目录，令注册与解引用一致。"""
    from forgeflow.config import get_settings

    monkeypatch.setattr(get_settings(), "resource_store_root", str(tmp_path / "res-blobs"))
    return tmp_path / "res-blobs"


async def _register_txt() -> tuple[str, str]:
    """注册一个真实 ``.txt`` 资源，返回 ``(资源 id, 原始注册名)``。"""
    from forgeflow.resources.summaries import content_kind

    assert content_kind("x.txt") == "text"  # 该资源解引用到 text_paths 平面
    service = ResourceService()
    name = "新建 文本文档 (2).txt"
    record = await service.register_file("t-inc55", name=name, data="验证内容\n".encode())
    return record.id, name


# --------------------------------------------------------------------------- #
# 1. 未声明资源 —— 提示词与改动前逐字节相同                                     #
# --------------------------------------------------------------------------- #
async def test_human_message_byte_for_byte_unchanged_without_resources():
    task = TaskCreate(intent="检查文件是否正确", context={})
    assert _human(task) == _PRE_CHANGE_HUMAN
    # 阳性对照：即便有其它 context 键（无 resources），仍是同一字节串。
    task2 = TaskCreate(intent="检查文件是否正确", context={"column": "amount"})
    assert _human(task2) == _PRE_CHANGE_HUMAN


async def test_block_omitted_when_resources_key_absent():
    ex = _executor()
    assert ex._resource_context(SimpleNamespace(context={})) == ""
    assert _RESOURCE_BLOCK_HEADER not in _human(TaskCreate(intent="x", context={}))


# --------------------------------------------------------------------------- #
# 2. 声明资源 —— 披露真实注册名 + 平面 + 读取工具 + 硬约束                       #
# --------------------------------------------------------------------------- #
async def test_declared_text_resource_is_disclosed_with_real_name_and_tool(
    memory_resources, blob_root
):
    rid, name = await _register_txt()
    task = TaskCreate(intent="检查文件是否正确，不正确进行修改", context={"resources": [rid]})

    human = _human(task)

    # 真实注册名（非伪造、非路径）确实进入提示词。
    assert name in human
    # 平面 + 读取工具指引。
    assert "文本文件" in human
    assert "textfile.inspect" in human
    # 硬约束句（禁止向用户索取内容/路径）。
    assert _RESOURCE_BLOCK_HEADER in human
    assert "禁止向用户索取文件内容或路径" in human
    # 注入硬化：资源名属不可信文本，走既有 UNTRUSTED 信封（sanitize_tool_output）。
    assert '<UNTRUSTED_TOOL_OUTPUT name="attached_resources">' in human
    assert "</UNTRUSTED_TOOL_OUTPUT>" in human


async def test_disclosures_never_leak_the_content_addressed_path(memory_resources, blob_root):
    """提示词只告知「有哪些资源 / 用哪个工具读」，**不**回吐真实路径。

    真实路径仍由平台在 ``_effective_args()`` 注入工具入参（缺陷 ① 的修复要求 4：
    不得让模型自造路径）。这里断言路径**不出现**在提示词里。
    """
    rid, name = await _register_txt()
    resolved = orch._resolve_resource_inputs({"resources": [rid]})
    real_path = resolved["paths"][0]

    human = _human(TaskCreate(intent="看下文件", context={"resources": [rid]}))
    assert name in human
    assert real_path not in human


async def test_block_has_a_hard_length_ceiling(monkeypatch):
    """过长/过多的资源名不得无界膨胀提示词（长度上限）。"""
    from forgeflow.runtime import react_executor as re

    huge = "A" * 5000
    monkeypatch.setattr(
        orch,
        "_resolve_resource_inputs",
        lambda context: {"text_paths": ["/p/x"], "text_names": [huge]},
    )
    task = TaskCreate(intent="大名字", context={"resources": ["res-any"]})
    human = _human(task)
    # header + 信封 + ≤ _RESOURCE_BLOCK_MAX_CHARS 正文 + 收尾信封，宽裕但不无界。
    assert len(human) < re._RESOURCE_BLOCK_MAX_CHARS + 400
    assert "<UNTRUSTED_TOOL_OUTPUT" in human


# --------------------------------------------------------------------------- #
# 3. 反事实 —— 断言有载荷（投毒后必须变红）                                      #
# --------------------------------------------------------------------------- #
def _positive_predicate(human: str, name: str) -> bool:
    """测试 2 的核心断言（被反事实测试复用，证明它**会**为假）。"""
    return name in human and "textfile.inspect" in human and _RESOURCE_BLOCK_HEADER in human


async def test_counterfactual_empty_dereference_removes_the_block(monkeypatch):
    """投毒：让 ``_resolve_resource_inputs`` 返回空 ⇒ 区块消失。

    若把测试 2 的 ``_positive_predicate`` 套在这个输出上，必须为 **False** ——
    这正是「断言有载荷、不是恒真」的可证伪证据。
    """
    monkeypatch.setattr(orch, "_resolve_resource_inputs", lambda context: {})
    task = TaskCreate(intent="看下文件", context={"resources": ["res-poisoned"]})
    human = _human(task)
    assert not _positive_predicate(human, "任何名字")
    assert _RESOURCE_BLOCK_HEADER not in human
    # 未投毒时该任务**会**进入资源分支（context.resources 非空）——反事实只改了
    # 解引用结果，所以上面的「消失」确由投毒导致。
    assert _executor()._resource_context(task) == ""


async def test_counterfactual_block_follows_the_dereference_not_a_constant(monkeypatch):
    """投毒：解引用返回**伪造名** ⇒ 提示词回显伪造名，且真实名不出现。

    证明区块内容**只**来自既有单一解引用，而不是任何常量 / 自带来源。
    """
    monkeypatch.setattr(
        orch,
        "_resolve_resource_inputs",
        lambda context: {"text_paths": ["/p/x"], "text_names": ["被投毒的名字.txt"]},
    )
    task = TaskCreate(intent="看下文件", context={"resources": ["res-poisoned"]})
    human = _human(task)
    assert "被投毒的名字.txt" in human  # 跟随解引用……
    assert "新建 文本文档 (2).txt" not in human  # ……而不是自带来源
