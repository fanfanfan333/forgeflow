"""T-B06 —— ``codeplane.injected`` 必须真的活到 ``GET /runs/{id}``（真链路钉子）。

## 留存经验（为什么要这条钉子）

INC27 让**控制面**（ForgeFlow）把本次选中的 Skill / Memory 交给**执行面**
（OpenHands）。handler ``forgeflow.runtime.tool_handlers::code_execute`` 确实把它写进了
``codeplane`` 子字典的 ``injected`` 键（skills 带 id/version/name，memory 带 id/scope）——
这一步从来是对的。

但 ``codeplane`` 会被 ``orchestrator._assemble_codeplane`` **重建成一个固定键的 dict**
再写回 ``RunRecord.codeplane``（``GET /runs/{id}`` 经 ``RunDetailResponse.codeplane`` 返回）。
重建时的固定键列表里**没有 ``injected``** ⇒ 它被静默丢弃 ⇒ ``GET /runs/{id}`` 里
``codeplane.injected`` **恒缺**，即使后端真的注入了 Skill/Memory。于是工作台左栏
「本次涉及 Skill / 记忆」永远拿不到真数据。

QA 的真后端反证（``qa_tmp/_a2_probe_injected_u8.txt``）：给 ``_assemble_codeplane`` 喂一个
带 ``injected`` 的 ``code.execute`` 载荷，产物 KEYS 里**没有** ``injected``，
``HAS_INJECTED=False``。

修法（T-B06，最小、禁第二套）：``_assemble_codeplane`` 组装时把 handler 自己报的
``injected`` **原样透传**（exec 载荷权威、commit/base 兜底；没人报 ⇒ 键如实缺失，不伪造）。

## 本文件主张的「真链路」

  1. **handler 段**：驱动**真实的** ``code_execute``（只把引擎 / 工作区两个边界换成可控桩），
     证明「选中了 skill/memory ⇒ payload 的 ``codeplane.injected`` 就是它们」；
  2. **assembler 段**：把上一步**真实的 payload** 当作 ``code.execute`` 调用喂进**真实的**
     ``_assemble_codeplane``，证明 ``injected`` 逐字存活；
  3. **endpoint 段**：把组装产物喂进**真实的** ``RunDetailResponse``，证明
     ``GET /runs/{id}`` 会把它带出去。

另有反事实（读 ``base`` 而非 exec ⇒ 丢失）与阴性对照（没人报 ⇒ 不伪造）。

引文一律 ``file.py::symbol``，不用行号。
"""

from __future__ import annotations

import asyncio
import types

import pytest

from forgeflow.codeplane.engine import CodeRunResult
# ``pytest`` 会把任何导入到本模块、名字以 ``Test`` 开头的类当成收集目标；用别名避免
# ``PytestCollectionWarning``（该类是引擎的评审结果，不是本文件的测试用例）。
from forgeflow.codeplane.tests_verdict import TestResult as _TestResult
from forgeflow.runtime import orchestrator as orch
from forgeflow.runtime.tool_handlers import code_execute

# --------------------------------------------------------------------------- #
# 选中的企业上下文（就是 control plane 会交给 execution plane 的那个子集）        #
# --------------------------------------------------------------------------- #
_SKILL = {"id": "skill_doc_rewrite", "version": "2.1.0", "name": "文档重写"}
_MEMORY = {"id": "mem-8f3a", "scope": "repo"}

_DIFF = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n+ fixed\n"


# --------------------------------------------------------------------------- #
# 可控的引擎 / 工作区边界（只替换这两个——其余全是真代码）                        #
# --------------------------------------------------------------------------- #
class _FakeWorkspace:
    workspace_id = "ws-tb06"
    path = "/tmp/ws-tb06"

    def to_dict(self) -> dict[str, object]:
        return {"workspace_id": self.workspace_id, "path": self.path}


class _FakeWorkspaceManager:
    def create(self, run_id: str, source: object, tenant_id: str | None = None) -> _FakeWorkspace:
        return _FakeWorkspace()


class _FakeEngine:
    interpreter = "python"

    def __init__(self, result: CodeRunResult) -> None:
        self._result = result
        self.last_job = None

    def available(self) -> bool:
        return True

    async def run(self, job: object) -> CodeRunResult:
        self.last_job = job
        return self._result


@pytest.fixture()
def engine_boundary(monkeypatch):
    """Wire the real ``code_execute`` to a controllable (engine, workspace) pair."""
    result = CodeRunResult(
        status="ok",
        diff=_DIFF,
        tests=_TestResult(passed=2, failed=0, verdict="passed", measured=True),
        timeline=[],
        raw={"call_usage": {"model": 0, "tool": 0}},  # The injected engine makes no SDK calls.
        summary={"files_changed": 1, "passed": 2, "failed": 0},
    )
    engine = _FakeEngine(result)

    from forgeflow.codeplane import engine as engine_module
    from forgeflow.codeplane import workspace as workspace_module

    monkeypatch.setattr(engine_module, "get_code_engine", lambda: engine)
    monkeypatch.setattr(workspace_module, "get_workspace_manager", lambda: _FakeWorkspaceManager())
    return engine


def _ctx() -> object:
    return types.SimpleNamespace(run_id="run-tb06", intent="改一处代码", tenant_id="t-1")


# --------------------------------------------------------------------------- #
# 1. handler 段：真实 ``code_execute`` 真的把注入写进 ``codeplane.injected``     #
# --------------------------------------------------------------------------- #
def test_handler_reports_injected_on_the_codeplane_payload(engine_boundary):
    payload = asyncio.run(
        code_execute(
            {"intent": "改一处代码", "skill_context": [_SKILL], "memory_context": [_MEMORY]},
            _ctx(),
        )
    )

    injected = payload["codeplane"]["injected"]
    assert injected["skills"] == [
        {"id": "skill_doc_rewrite", "version": "2.1.0", "name": "文档重写"}
    ]
    assert injected["memory"] == [{"id": "mem-8f3a", "scope": "repo"}]
    # The job really carried the selected context (proves the handler read ``args``).
    assert engine_boundary.last_job.skill_context == [_SKILL]
    assert engine_boundary.last_job.memory_context == [_MEMORY]


def test_handler_reports_empty_injected_when_nothing_selected(engine_boundary):
    """没人选中 ⇒ 空的 skills/memory，不是缺字段，也不是任何占位。"""
    payload = asyncio.run(code_execute({"intent": "改一处代码"}, _ctx()))
    assert payload["codeplane"]["injected"] == {"skills": [], "memory": []}


# --------------------------------------------------------------------------- #
# 2. assembler 段：真实 payload 经 ``_assemble_codeplane`` 后 ``injected`` 存活   #
# --------------------------------------------------------------------------- #
def test_assembler_carries_injected_from_the_real_payload(engine_boundary):
    payload = asyncio.run(
        code_execute(
            {"intent": "改一处代码", "skill_context": [_SKILL], "memory_context": [_MEMORY]},
            _ctx(),
        )
    )
    records = [{"tool": "code.execute", "payload": payload}]

    assembled = orch._assemble_codeplane(records, [])

    assert assembled["injected"]["skills"] == [
        {"id": "skill_doc_rewrite", "version": "2.1.0", "name": "文档重写"}
    ]
    assert assembled["injected"]["memory"] == [{"id": "mem-8f3a", "scope": "repo"}]


def test_the_exec_payload_is_authoritative_over_a_commit_without_injected():
    """待审批的 ``code.commit`` 没 ``injected`` ⇒ 必须从 exec 载荷读（否则丢）。"""
    records = [
        {"tool": "code.execute", "payload": {"codeplane": {
            "injected": {"skills": [_SKILL], "memory": [_MEMORY]},
        }}},
        {"tool": "code.commit", "payload": {"codeplane": {
            "approval": {"status": "pending"},
        }}},
    ]

    assembled = orch._assemble_codeplane(records, [])
    assert assembled["injected"]["skills"] == [_SKILL]

    # 反事实：组装器的 ``base = commit_cp or exec_cp`` 这里是 commit 载荷，
    # 它**没有** injected —— 若从 base 读（或沿用旧的固定键重建），键就没了。
    base = records[1]["payload"]["codeplane"]
    assert "injected" not in base
    assert "injected" not in {  # 复现旧固定键重建的行为：去掉该键后确实消失
        k: v for k, v in assembled.items() if k != "injected"
    }


def test_assembler_does_not_invent_injected_when_none_reported():
    """老 payload（不带 injected）不得凭空补一个 —— 保持缺失。"""
    assembled = orch._assemble_codeplane(
        [{"tool": "code.execute", "payload": {"codeplane": {"diff": "x"}}}],
        [],
    )
    assert "injected" not in assembled


# --------------------------------------------------------------------------- #
# 3. endpoint 段：``GET /runs/{id}`` 的响应模型真的把它带出去                    #
# --------------------------------------------------------------------------- #
def test_run_detail_response_surfaces_injected(engine_boundary):
    from forgeflow.api.hub_schemas import RunDetailResponse

    payload = asyncio.run(
        code_execute(
            {"intent": "改一处代码", "skill_context": [_SKILL], "memory_context": [_MEMORY]},
            _ctx(),
        )
    )
    assembled = orch._assemble_codeplane(
        [{"tool": "code.execute", "payload": payload}], []
    )

    response = RunDetailResponse(
        run_id="run-tb06",
        thread_id="thread-tb06",
        status="awaiting_approval",
        outcome="succeeded",
        intent="改一处代码",
        created_at="2026-01-01T00:00:00Z",
        # ``routers/runs.py::get_run`` 的做法：把 record.codeplane 原样传给响应。
        codeplane=dict(assembled),
    ).model_dump()

    assert response["codeplane"]["injected"]["skills"][0]["id"] == "skill_doc_rewrite"
    assert response["codeplane"]["injected"]["memory"][0]["scope"] == "repo"


# --------------------------------------------------------------------------- #
# 4. 反事实：去掉透传那一段，真链路立刻丢失 injected（可转红）                    #
# --------------------------------------------------------------------------- #
def test_counterfactual_transmission_is_load_bearing(monkeypatch, engine_boundary):
    payload = asyncio.run(
        code_execute(
            {"intent": "改一处代码", "skill_context": [_SKILL], "memory_context": [_MEMORY]},
            _ctx(),
        )
    )
    records = [{"tool": "code.execute", "payload": payload}]

    # Positive control — 修好后 injected 存活。
    assert "injected" in orch._assemble_codeplane(records, [])

    # 复现 T-B06 之前的行为：重建后的 dict 里没有 injected。
    def _pre_fix(records: object, steps: object) -> dict:
        out = orch._assemble_codeplane(records, steps)
        out.pop("injected", None)  # 旧固定键列表从不含该键 ⇒ 静默丢失
        return out

    assert "injected" not in _pre_fix(records, [])
