"""INC12 A7 — 孤儿工具（orphan tool）纳管守卫 (QA2 / yan-guoguan).

结论先行
--------
* 本仓**没有** ``agent_tools.json``。全仓递归检索文件名 ``agent_tools*.json`` 命中 0；
  全仓文本检索字面量 ``agent_tools`` 命中 0（见 ``qa_tmp/a7_find_agent_tools.txt``
  与 ``qa_tmp/a7_agent_tools_name.txt``）。因此"前端工作区工具清单"在本仓不可得，
  A7 的判定基准改为**运行时真实绑定** ``forgeflow.runtime.tool_registry``。
* 主理人列出的 7 个候选 id：
  - ``policy.check`` / ``git.diff`` / ``code.lint`` → **真实现**，
    ``tool_registry.py:144-164`` 注册 ``kind="real"``，且在
    ``gate.py:71-73`` 的 ``PLATFORM_TOOL_CATALOGUE`` 内 → 非孤儿；
  - ``bug.vulcan.fetch`` / ``bug.vulcan.scan`` / ``hub.modifySkill`` /
    ``hub.createSubAgent`` → 全仓文本检索命中 **0**（``qa_tmp/a7_orphan_grep.txt``），
    本仓既无声明也无实现 → 判为**不受本仓纳管（外部系统归属）**。

纳管方式（选 c：排除归属）不是"眼不见为净"，而是给它们装上机制：
这类 id 在本仓 ``resolve()`` 必为 ``None``，``ToolExecutor`` 因此落
``unavailable`` / ``executed=False`` —— **永远不可能被上报成 ok**。
本文件把这条钉死，任何人之后把它们写进 plan 都只能得到一个诚实的 unavailable。

数据来源一句话说明：以上 4 个 id 的存在性证据只有主理人 brief 中的一句话，
本仓零命中；我无法验证它们在外部系统里是否真有实现，故不下"是真工具"的结论。
"""

from __future__ import annotations

import pytest

from forgeflow.runtime import tool_registry
from forgeflow.runtime.gate import PLATFORM_PLAN_TOOLS, required_permission
from forgeflow.runtime.tool_executor import ToolCallContext, ToolExecutor
from forgeflow.runtime.tool_registry import UNBOUND_TOOLS
from forgeflow.skills.candidate_compiler import _FALLBACK_TOOLS
from forgeflow.skills.registry import _FEATURED_SEED

pytestmark = pytest.mark.asyncio

#: 主理人列出、但本仓零命中的 4 个 id —— 归属外部系统（bug-vulcan / hub）。
UNOWNED_EXTERNAL_IDS: frozenset[str] = frozenset(
    {"bug.vulcan.fetch", "bug.vulcan.scan", "hub.modifySkill", "hub.createSubAgent"}
)

#: 本仓确认为真实现的 3 个候选 id。
CONFIRMED_REAL_CANDIDATES: frozenset[str] = frozenset(
    {"policy.check", "git.diff", "code.lint"}
)


@pytest.fixture(autouse=True)
def _clean_registry():
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()
    yield
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()


def _ctx(**kw) -> ToolCallContext:
    base = dict(
        run_id="run-a7",
        step_id="run-a7:0:0",
        tenant_id="t-a7",
        user_id="u-a7",
        role="admin",
        intent="审计工具目录",
        attempt=0,
        args={},
    )
    base.update(kw)
    return ToolCallContext(**base)


# --------------------------------------------------------------------------- #
# 1. 目录 ↔ 绑定 一一对应：既不能有孤儿，也不能有无人认领的绑定           #
# --------------------------------------------------------------------------- #
async def test_platform_catalogue_and_registered_bindings_are_the_same_set(
    force_memory_backend,
):
    """``PLATFORM_PLAN_TOOLS`` 与注册表 ``known_ids()`` 必须是同一个集合。

    差集为空才叫"零孤儿"：
    * 目录里有、注册表里没有 ⇒ 声明了一个没有实现的工具（孤儿）；
    * 注册表里有、目录里没有 ⇒ 实现了一个永远不会被放行的工具（死绑定）。
    """
    known = set(tool_registry.known_ids())
    declared = set(PLATFORM_PLAN_TOOLS)

    assert declared - known == set(), f"孤儿工具（有声明无实现）: {sorted(declared - known)}"
    assert known - declared == set(), f"死绑定（有实现无声明）: {sorted(known - declared)}"

    for tool_id in sorted(known):
        binding = tool_registry.resolve(tool_id)
        assert binding is not None, tool_id
        assert callable(binding.handler), f"{tool_id} 的 handler 不可调用"
        assert binding.kind in {"real", "development"}, f"{tool_id} kind={binding.kind}"
        assert binding.provider, f"{tool_id} 没有 provider"


async def test_every_declared_tool_id_resolves_to_a_binding_or_is_explicitly_unbound(
    force_memory_backend,
):
    """本仓任何位置声明过的工具 id，要么有实现，要么在 ``UNBOUND_TOOLS`` 里。

    声明来源（三处，都是真的会被 plan 用到的）：
    * ``skills/registry.py:_FEATURED_SEED`` 四个种子技能；
    * ``runtime/gate.py:PLATFORM_PLAN_TOOLS``；
    * ``skills/candidate_compiler.py:_FALLBACK_TOOLS`` 兜底 plan。
    """
    declared: set[str] = set(PLATFORM_PLAN_TOOLS) | set(_FALLBACK_TOOLS)
    for seed in _FEATURED_SEED:
        declared |= set(seed["tools"])

    assert declared, "反空转：声明集合为空，这条断言会空转成绿"

    orphans = sorted(declared - set(tool_registry.known_ids()) - set(UNBOUND_TOOLS))
    assert orphans == [], f"本仓出现孤儿工具: {orphans}"


# --------------------------------------------------------------------------- #
# 2. 三个"候选孤儿"其实是真实现 —— 钉住，防止回退成占位                    #
# --------------------------------------------------------------------------- #
async def test_the_three_catalogue_candidates_are_real_implementations(
    force_memory_backend,
):
    """``policy.check`` / ``git.diff`` / ``code.lint`` 必须是 ``kind="real"``。

    这三个 id 曾在历史上是"目录里有、实现是占位"。此断言要求它们的 provider
    落在一个真实实现上，而不是 ``development-stub`` 之类的自述桩件。
    """
    for tool_id in sorted(CONFIRMED_REAL_CANDIDATES):
        binding = tool_registry.resolve(tool_id)
        assert binding is not None, f"{tool_id} 没有绑定 —— 退回孤儿状态"
        assert binding.kind == "real", f"{tool_id} kind={binding.kind}（应为 real）"
        assert binding.provider != "development-stub", f"{tool_id} provider={binding.provider}"
        assert tool_id in PLATFORM_PLAN_TOOLS


# --------------------------------------------------------------------------- #
# 3. 四个无归属 id：不纳管，但必须 fail-closed，永不上报 ok                 #
# --------------------------------------------------------------------------- #
async def test_unowned_external_ids_have_no_binding(force_memory_backend):
    """外部系统的 4 个 id 在本仓 ``resolve()`` 必须为 ``None``。"""
    for tool_id in sorted(UNOWNED_EXTERNAL_IDS):
        assert tool_registry.resolve(tool_id) is None, f"{tool_id} 竟被绑定了实现"
        assert tool_id not in PLATFORM_PLAN_TOOLS, f"{tool_id} 混进了平台白名单"


async def test_unowned_external_ids_fail_closed_to_unavailable(force_memory_backend):
    """把一个无归属 id 塞进 plan：只能得到 ``unavailable``，绝不能是 ok。

    这是"排除归属"真正的机制：不是删掉名字假装它不存在，而是让它在运行时
    老实回答"我没有实现"。
    """
    for tool_id in sorted(UNOWNED_EXTERNAL_IDS):
        inv = await ToolExecutor().execute(tool_id, ctx=_ctx())
        assert inv.status == "unavailable", f"{tool_id} status={inv.status}（应为 unavailable）"
        assert inv.executed is False, f"{tool_id} 被上报为已执行"
        assert inv.result_ref is None
        assert inv.error, f"{tool_id} unavailable 却没有说明原因"


async def test_unowned_external_ids_are_rbac_fail_closed(force_memory_backend):
    """无归属 id 走 RBAC 的 fail-closed 分支：``execute:<namespace>``。"""
    for tool_id in sorted(UNOWNED_EXTERNAL_IDS):
        action, resource = required_permission(tool_id)
        assert (action, resource) == ("execute", tool_id.split(".")[0]), tool_id
        # 绝不能退化成粗粒度的 execute:workflows（那等于放行）
        assert resource != "workflows", f"{tool_id} 退化成了 execute:workflows"


# --------------------------------------------------------------------------- #
# 4. 本守卫自身的反空转自检                                                  #
# --------------------------------------------------------------------------- #
async def test_orphan_guard_itself_is_not_vacuous(force_memory_backend):
    """如果注册表是空的/坏的，上面几条"没有孤儿"会空转成绿 —— 这里堵住它。

    阳性对照：一个已知真实现必须解析成功；阴性对照：一个已知无实现必须失败。
    两者同时成立，才说明上面的断言真的在比较，而不是在对着一个空集合点头。
    """
    assert UNOWNED_EXTERNAL_IDS, "反空转：外部 id 清单为空"
    assert CONFIRMED_REAL_CANDIDATES, "反空转：真实现清单为空"

    assert len(tool_registry.known_ids()) >= len(CONFIRMED_REAL_CANDIDATES)

    positive = tool_registry.resolve(sorted(CONFIRMED_REAL_CANDIDATES)[0])
    assert positive is not None, "阳性对照失败：真实现也解析不出来"

    negative = await ToolExecutor().execute(sorted(UNOWNED_EXTERNAL_IDS)[0], ctx=_ctx())
    assert negative.status == "unavailable", "阴性对照失败：无归属 id 没有 fail-closed"
