"""INC46 T12 — LangGraph Skill Subgraph（feature-flag）单元测试。

本文件真跑（不是重实现）：

* **feature flag** — ``FORGEFLOW_SKILL_SUBGRAPH`` 默认 **关闭**；仅 ``1/true/yes/on``
  为开。开关在**调用时**读取（可逐测试翻转）。
* **共用状态、不复制** — 子图持有调用方**同一个** ``SkillRunState`` 对象
  （``SkillSubgraph.state is main_state``），节点原地改，run 结束时主图与子图口径
  必然一致（``reconcile``）。
* **复用 T21（不另造暂停机制）** — 暂停/恢复走 ``forgeflow.hitl.pending`` 的
  ``PendingAction`` store；未 resolve ⇒ **不得继续**；过期 ⇒ fail-closed。
* **阳性** — flag 开 ⇒ 子图跑通一条含 ``pause → resolve → 继续`` 的 skill 路径，
  最终状态与内联口径**逐字节一致**。
* **阴性** — flag 关 ⇒ 走内联路径、**零回归**（图从未被进入，``graph_run_count==0``）；
  子图抛错 ⇒ 共享状态被还原，主图**不被污染**；暂停未 resolve ⇒ 不继续。
* **反事实（真跑，登记）** — ``test_flag_off_uses_inline_path_zero_regression`` 是登记
  的红证目标：把 ``skill_subgraph.py::skill_subgraph_enabled`` 的默认由 ``"0"`` 改成
  ``"1"`` 后本用例必须转红（它 ``delenv`` 后依赖默认值，默认变 True 即走图路径）。
* **红线 18** — 执行循环有预算与终止条件（``test_execution_budget_halts_run``）。
* **红线 21** — 工具门（白名单 / RBAC / HITL policy）绝不被绕过
  （``test_denied_tool_never_executes`` / ``test_non_whitelist_tool_denied``）。
* **红线 10** — langgraph 缺席 ⇒ 诚实 ``skipped``，绝不假装执行。

引文纪律：一律 ``file.py::symbol``，不写行号。
"""

from __future__ import annotations

import json

import pytest

from forgeflow.hitl import pending as P
from forgeflow.skills import skill_subgraph as S
from forgeflow.skills.runtime import SkillStep

_TENANT = "t-inc46-t12"


# --------------------------------------------------------------------------- #
# fixtures / helpers                                                           #
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _fresh_store():
    """Pin a fresh in-memory pending store per test (offline, no PG)."""
    store = P.InMemoryPendingActionStore()
    P.set_pending_store(store)
    S.reset_graph_run_count()
    yield store
    S.reset_graph_run_count()
    P.reset_pending_store()


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch):
    """Start every test with the flag **unset** (default-off)."""
    monkeypatch.delenv(S.FEATURE_FLAG_ENV, raising=False)


def _step(tool: str, purpose: str = "") -> SkillStep:
    return SkillStep(purpose=purpose or f"step {tool}", tool=tool)


def _state(*, run_id: str = "run-1", role: str = "sales_rep", tools=None) -> S.SkillRunState:
    return S.SkillRunState(
        run_id=run_id,
        tenant_id=_TENANT,
        role=role,
        steps=[_step(t) for t in (tools or ["data.query"])],
    )


def _canon(state: S.SkillRunState) -> str:
    return json.dumps(state.to_dict(), sort_keys=True, ensure_ascii=False)


def _ok_runner(seen: list):
    def _run(step: SkillStep, state: S.SkillRunState):
        seen.append((step.tool, id(state)))
        return {"tool": step.tool, "ok": True}

    return _run


# --------------------------------------------------------------------------- #
# 1. feature flag                                                              #
# --------------------------------------------------------------------------- #
def test_flag_default_is_off():
    """默认关闭：环境变量未设 ⇒ False。"""
    assert S.skill_subgraph_enabled() is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", True), ("true", True), ("YES", True), ("On", True), ("0", False), ("", False), ("no", False)],
)
def test_flag_reads_env_truthy_values(monkeypatch, raw, expected):
    """仅 ``1/true/yes/on``（大小写不敏感）为开，其余为关。"""
    monkeypatch.setenv(S.FEATURE_FLAG_ENV, raw)
    assert S.skill_subgraph_enabled() is expected


def test_langgraph_probe_reports_availability_and_version():
    """探针如实报告 langgraph 可用性与版本（不可用 ⇒ 版本为 None，不伪造）。"""
    assert isinstance(S.langgraph_available(), bool)
    version = S.langgraph_version()
    if S.langgraph_available():
        assert isinstance(version, str) and version
    else:
        assert version is None


# --------------------------------------------------------------------------- #
# 2. shared state — no independent copy                                        #
# --------------------------------------------------------------------------- #
async def test_subgraph_shares_state_object_not_a_copy():
    """子图持有的是调用方的**同一个**对象，节点看到的就是它（不复制状态）。"""
    main = _state(tools=["data.query", "analysis.score"])
    sub = S.SkillSubgraph(main)
    assert sub.state is main  # 不复制
    seen: list = []
    sub2 = S.SkillSubgraph(main, runner=_ok_runner(seen))
    await sub2.run()
    assert sub2.state is main
    assert seen and all(oid == id(main) for _tool, oid in seen)


async def test_reconcile_main_and_subgraph_agree():
    """run 结束时主图与子图对同一 run 的最终状态一致（无两个真相）。"""
    main = _state(tools=["data.query"])
    result = await S.run_skill_subgraph(main, runner=_ok_runner([]))
    assert S.reconcile(main, result) is True
    assert result.final_projection == main.to_dict()


# --------------------------------------------------------------------------- #
# 3. flag OFF ⇒ inline path, zero regression (反事实目标)                       #
# --------------------------------------------------------------------------- #
async def test_flag_off_uses_inline_path_zero_regression():
    """flag 关 ⇒ 走内联路径、图从未被进入，输出与直接内联逐字节一致。

    反事实（登记）：把 ``skill_subgraph_enabled`` 默认改成 True 后，
    本用例会走图路径（``path == "graph"``），断言 ``path == "inline"`` 转红。
    """
    main = _state(tools=["data.query", "analysis.score"])
    clone = _state(tools=["data.query", "analysis.score"])
    before = S.graph_run_count()
    result = await S.execute_skill_run(main, runner=_ok_runner([]))  # 依赖默认（关）
    assert result.path == S.PATH_INLINE
    assert S.graph_run_count() == before  # 图从未被进入
    # 与直接内联路径逐字节一致
    inline = await S.execute_skill_run(clone, runner=_ok_runner([]), use_graph=False)
    assert S.PATH_INLINE == inline.path
    assert _canon(main) == _canon(clone)
    assert result.final_projection == inline.final_projection


async def test_flag_on_runs_graph_and_off_matches_for_non_pausing_run(monkeypatch):
    """flag 开 ⇒ 真跑图路径；对同一（无暂停）输入，开/关两路径最终状态逐字节一致。"""
    monkeypatch.setenv(S.FEATURE_FLAG_ENV, "1")
    on_state = _state(tools=["data.query", "analysis.score"])
    before = S.graph_run_count()
    on = await S.execute_skill_run(on_state, runner=_ok_runner([]))
    assert on.path == S.PATH_GRAPH
    assert S.graph_run_count() == before + 1

    off_state = _state(tools=["data.query", "analysis.score"])
    off = await S.execute_skill_run(off_state, runner=_ok_runner([]), use_graph=False)
    assert on.status == off.status == S.DONE
    assert _canon(on_state) == _canon(off_state)


# --------------------------------------------------------------------------- #
# 4. positive — pause → resolve → continue (graph)                             #
# --------------------------------------------------------------------------- #
async def test_positive_pause_resolve_continue_graph():
    """子图跑通一条含一次 pause→resolve→继续 的 skill 路径，最终与主图口径一致。"""
    steps_tools = ["data.query", "sheet.edit", "analysis.score"]  # WRITE 在中间
    main = _state(tools=steps_tools)
    seen: list = []

    # #1 首跑：在第 2 步（WRITE）暂停，等待人工
    r1 = await S.execute_skill_run(main, runner=_ok_runner(seen), use_graph=True)
    assert r1.path == S.PATH_GRAPH
    assert r1.status == S.PAUSED
    assert main.pending_id is not None
    assert main.cursor == 1 and len(main.executed) == 1

    # 复核：暂停动作真的是 T21 store 里的一条 waiting 记录
    action = P.get_pending_store().get(_TENANT, main.pending_id)
    assert action is not None and action.status == P.WAITING

    # 人工 resolve
    P.get_pending_store().resolve(_TENANT, main.pending_id, decision="approve", actor="u1")

    # #2 恢复：继续执行剩余步骤直至 done
    r2 = await S.execute_skill_run(main, runner=_ok_runner(seen), use_graph=True)
    assert r2.status == S.DONE
    assert main.cursor == 3 and len(main.executed) == 3
    assert [e["tool"] for e in main.executed] == steps_tools
    assert S.reconcile(main, r2) is True

    # 内联路径在同样输入 + 同样 resolve 下得到逐字节一致的最终状态
    inline = _state(tools=steps_tools)
    await S.execute_skill_run(inline, runner=_ok_runner([]), use_graph=False)
    P.get_pending_store().resolve(_TENANT, inline.pending_id, decision="approve", actor="u1")
    ri = await S.execute_skill_run(inline, runner=_ok_runner([]), use_graph=False)
    assert ri.status == S.DONE
    assert _canon(main) == _canon(inline)


async def test_paused_unresolved_does_not_continue():
    """暂停未 resolve ⇒ 不得继续执行（cursor / executed 均不变，状态仍 paused）。"""
    main = _state(tools=["data.query", "sheet.edit", "analysis.score"])
    await S.execute_skill_run(main, runner=_ok_runner([]), use_graph=True)
    assert main.status == S.PAUSED
    cursor_before, exec_before = main.cursor, len(main.executed)

    resumed = await S.execute_skill_run(main, runner=_ok_runner([]), use_graph=True)
    assert resumed.status == S.PAUSED
    assert main.cursor == cursor_before
    assert len(main.executed) == exec_before


async def test_expired_pending_fails_closed():
    """暂停动作过期 ⇒ fail-closed 终止，绝不隐式通过、绝不继续。"""
    main = _state(tools=["data.query", "sheet.edit"])
    await S.execute_skill_run(main, runner=_ok_runner([]), use_graph=True)
    assert main.status == S.PAUSED
    # 直接把该 pending 置为过期（用 T21 自身的到期扫描）
    store = P.get_pending_store()
    store.expire_overdue(_TENANT, now=action_now(store, main.pending_id))

    resumed = await S.execute_skill_run(main, runner=_ok_runner([]), use_graph=True)
    assert resumed.status == S.EXPIRED_STATUS
    assert len(main.executed) == 1  # 未继续执行第二步


# --------------------------------------------------------------------------- #
# 5. negative — a failing subgraph must not pollute the main state             #
# --------------------------------------------------------------------------- #
async def test_subgraph_error_does_not_pollute_main_state():
    """子图抛错 ⇒ 共享状态被还原，主图状态逐字节不变。"""
    main = _state(tools=["data.query", "analysis.score"])

    def _boom(step: SkillStep, state: S.SkillRunState):
        raise RuntimeError("runner exploded")

    snapshot = _canon(main)
    result = await S.run_skill_subgraph(main, runner=_boom)
    assert result.status == S.FAILED
    assert "RuntimeError" in (result.reason or "")
    assert _canon(main) == snapshot  # 未被污染
    assert main.status == S.RUNNING and main.cursor == 0 and main.executed == []


# --------------------------------------------------------------------------- #
# 6. red lines — budget (18) and the tool gate (21)                            #
# --------------------------------------------------------------------------- #
async def test_execution_budget_halts_run():
    """红线 18：执行预算被触顶 ⇒ halted（有终止条件，绝不无限循环）。"""
    main = _state(tools=["data.query", "analysis.score", "docs.parse"])
    result = await S.run_skill_subgraph(main, runner=_ok_runner([]), max_iterations=1)
    assert result.status == S.HALTED
    assert main.status == S.HALTED
    assert len(main.executed) == 1  # 只跑了预算内的一步


async def test_denied_tool_never_executes():
    """红线 21：DANGEROUS 工具（code.commit）默认拒绝 ⇒ 不产生执行、不暂停。"""
    main = _state(role="sales_rep", tools=["code.commit"])
    seen: list = []
    result = await S.run_skill_subgraph(main, runner=_ok_runner(seen))
    assert result.status == S.DENIED
    assert main.executed == []
    assert seen == []  # runner 从未被调用


async def test_non_whitelist_tool_denied():
    """红线 21：白名单外工具 ⇒ 拒绝执行（verbatim 原因），绝不替换。"""
    main = _state(tools=["totally.unknown"])
    result = await S.run_skill_subgraph(main, runner=_ok_runner([]))
    assert result.status == S.DENIED
    assert "不在平台白名单内" in (main.reason or "")
    assert main.executed == []


# --------------------------------------------------------------------------- #
# 7. honest degradation (红线 10)                                              #
# --------------------------------------------------------------------------- #
async def test_skipped_when_langgraph_unavailable(monkeypatch):
    """langgraph 缺席 ⇒ 诚实 skipped，共享状态一字不动，绝不假装跑过。"""
    main = _state(tools=["data.query"])
    snapshot = _canon(main)
    monkeypatch.setattr(S, "_LANGGRAPH_AVAILABLE", False)
    result = await S.run_skill_subgraph(main, runner=_ok_runner([]))
    assert result.path == S.PATH_SKIPPED
    assert result.status == S.SKIPPED and result.degraded is True
    assert _canon(main) == snapshot


async def test_missing_runner_is_honest_unavailable():
    """无 runner ⇒ 该步记为 unavailable 并终止（诚实降级，不伪造成功）。"""
    main = _state(tools=["data.query"])
    result = await S.run_skill_subgraph(main, runner=None)
    assert result.status == S.FAILED
    assert main.executed and main.executed[0]["status"] == "unavailable"


# --------------------------------------------------------------------------- #
# helper — build a "now" past a pending's deadline (T21 usage, not a re-impl)  #
# --------------------------------------------------------------------------- #
def action_now(store: P.PendingActionStore, pending_id: str):
    """A datetime strictly past ``pending_id``'s deadline (drives real expiry)."""
    from datetime import timedelta

    action = store.get(_TENANT, pending_id)
    assert action is not None and action.expires_at is not None
    return action.expires_at + timedelta(seconds=1)
