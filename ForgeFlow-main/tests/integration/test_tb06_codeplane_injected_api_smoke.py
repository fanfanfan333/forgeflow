"""T-B06 冒烟（真后端 / REST 缝）—— ``GET /runs/{id}.codeplane.injected`` 真的出现。

## 这条冒烟在防什么

QA 的反证（``qa_tmp/_a2_probe_injected_u8.txt``）是**直接调** ``_assemble_codeplane``
得到的：证明组装器会丢 ``injected``。本文件把证据提升到**真后端接缝**：
真实 ``orchestrator.run_task``（真 planner → 真 input admission → 真 RBAC/Policy →
真 ``ToolExecutor`` → 真 ``tool_handlers.code_execute`` → 真 ``_assemble_codeplane`` →
真 ``RunRecord``）+ 真实 ``GET /runs/{id}`` 路由（``routers/runs.py::get_run`` →
``RunDetailResponse``）。

## 被替换的边界（**只有**这些，且都是环境缺失，不是被测行为）

  * ``forgeflow.codeplane.engine.get_code_engine`` /
    ``forgeflow.codeplane.workspace.get_workspace_manager`` —— 本机无 OpenHands venv /
    隔离工作区；换成**可用**的引擎 + 工作区桩，使 ``code_execute`` 走到**真实运行分支**
    （该分支才产出 ``injected``）。关键：``injected`` 由**真实 handler** 从
    ``job.skill_context`` 产出，**绝非**本桩手写。
  * ``orchestrator._resolve_injected_skills`` 与 ``orchestrator._build_run_context`` ——
    「选中的 skill 从哪来」这一步依赖 **DB 技能仓储**（本机无）。属**仓库既有做法**
    （见 ``tests/unit/test_inc46_skill_subgraph.py`` 对同名函数打桩）：只替换**输入**
    （哪些 skill 被选中），被测的「``injected`` 能否活到端点」链路全真。

  ⚠️ 为什么不用 ``task.context["skill_ids"]``：该键在 ``run_task`` 里先经
  ``input_scope.prepare_input_scope`` 对**真实技能仓储**做准入校验（不存在即 404），
  与「assembler 是否丢 injected」这一缺陷**无关**，且本机无该仓储。故 skill 的
  **选中**通过上下文构建同一条缝注入，语义等价（``ctx.injected_skills`` 的同一来源）。

## 反事实（登记）

把 ``orchestrator._assemble_codeplane`` 里 T-B06 透传 ``injected`` 的那几行删掉，
``test_injected_reaches_the_run_detail_endpoint`` 必须转红（``codeplane.injected``
重新恒缺）—— 正是 QA 反证复现的旧行为。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.routers import runs as runs_router
from forgeflow.codeplane.tests_verdict import TestResult as _TestResult
from forgeflow.runtime import orchestrator as orch
from forgeflow.runtime.orchestrator import (
    RequestContext,
    TaskCreate,
    reset_run_store,
    run_task,
)

pytestmark = pytest.mark.asyncio

TENANT = "t-tb06"
INTENT = "修复登录 500"
SKILL_ID = "skill_tb06_smoke"
SKILL = {
    "id": SKILL_ID,
    "name": "冒烟技能",
    "version": "1.2.0",
    "steps": ["改代码", "跑测试"],
}


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, run_id: str, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


@pytest.fixture(autouse=True)
def _clean_store():
    reset_run_store()
    yield
    reset_run_store()


# --------------------------------------------------------------------------- #
# 被替换的边界（见文件头；只有这些）                                             #
# --------------------------------------------------------------------------- #
class _FakeWorkspace:
    workspace_id = "ws-tb06-smoke"
    path = "/tmp/ws-tb06-smoke"

    def to_dict(self) -> dict[str, object]:
        return {"workspace_id": self.workspace_id, "path": self.path}


class _FakeWorkspaceManager:
    def create(self, run_id: str, source: object, tenant_id: str | None = None) -> _FakeWorkspace:
        return _FakeWorkspace()


class _FakeEngine:
    interpreter = "python"

    def available(self) -> bool:
        return True

    async def run(self, job: object):
        from forgeflow.codeplane.engine import CodeRunResult

        return CodeRunResult(
            status="ok",
            diff="diff --git a/login.py b/login.py\n--- a/login.py\n+++ b/login.py\n+ fixed\n",
            tests=_TestResult(passed=2, failed=0, verdict="passed", measured=True),
            timeline=[],
            raw={"call_usage": {"model": 0, "tool": 0}},  # The injected engine makes no SDK calls.
            summary={"files_changed": 1, "passed": 2, "failed": 0},
        )


@pytest.fixture()
def boundaries(monkeypatch):
    """Replace the engine/workspace boundary **and** the DB-backed context seam.

    Returns a mutable ``state``; set ``state['skill'] = None`` to model "nothing
    was selected" (the honest empty case).
    """
    from forgeflow.codeplane import engine as engine_module
    from forgeflow.codeplane import workspace as workspace_module

    monkeypatch.setattr(engine_module, "get_code_engine", lambda: _FakeEngine())
    monkeypatch.setattr(workspace_module, "get_workspace_manager", lambda: _FakeWorkspaceManager())

    state: dict[str, object] = {"skill": dict(SKILL)}

    async def _build_context(task, ctx, run_id):  # noqa: ANN001
        skill = state["skill"]
        if skill:
            return SimpleNamespace(
                sections=[{"source": "skill", "ref_id": SKILL_ID, "text": "冒烟技能"}]
            )
        return SimpleNamespace(sections=[])

    async def _resolve_skills(tenant_id, skill_ids):  # noqa: ANN001
        skill = state["skill"]
        ids = [str(s) for s in (skill_ids or []) if str(s or "").strip()]
        return [dict(skill)] if (skill and SKILL_ID in ids) else []

    monkeypatch.setattr(orch, "_build_run_context", _build_context)
    monkeypatch.setattr(orch, "_resolve_injected_skills", _resolve_skills)
    return state


def _detail(handle) -> dict:  # noqa: ANN001
    """Read the run back through the **real** ``GET /runs/{id}`` route."""
    app = FastAPI()
    app.dependency_overrides[resolve_tenant] = lambda: TENANT
    app.include_router(runs_router.router, prefix="/runs")
    response = TestClient(app).get(f"/runs/{handle.run_id}")
    assert response.status_code == 200, response.text
    return response.json()


async def _run_code_task(repo_path: str) -> dict:
    handle = await run_task(
        TaskCreate(
            intent=INTENT,
            workflow_type="generic",
            context={"declared_tools": ["code.execute"], "repo_path": repo_path},
        ),
        RequestContext(tenant_id=TENANT, user_id="u-tb06", role="admin"),
        bus=_RecordingBus(),
    )
    return _detail(handle)


# --------------------------------------------------------------------------- #
# 冒烟：带选中 skill 的代码任务 → GET /runs/{id} 里 codeplane.injected 真的出现   #
# --------------------------------------------------------------------------- #
async def test_injected_reaches_the_run_detail_endpoint(boundaries, force_memory_backend, tmp_path):
    body = await _run_code_task(str(tmp_path))

    codeplane = body["codeplane"]
    # 打印原始证据（用 -s 跑以落 qa_tmp）。
    print("SMOKE.codeplane.injected =", json.dumps(codeplane.get("injected"), ensure_ascii=False))

    assert "injected" in codeplane, "组装器又把 injected 丢了 —— T-B06 回归"
    assert codeplane["injected"]["skills"] == [
        {"id": SKILL_ID, "version": "1.2.0", "name": "冒烟技能"}
    ]
    assert codeplane["injected"]["memory"] == []


async def test_empty_injection_is_present_and_honest(boundaries, force_memory_backend, tmp_path):
    """没选 skill ⇒ ``injected`` 仍在（键出现），内容**如实为空**（不伪造）。"""
    boundaries["skill"] = None
    body = await _run_code_task(str(tmp_path))

    codeplane = body["codeplane"]
    print("SMOKE.empty.codeplane.injected =", json.dumps(codeplane.get("injected"), ensure_ascii=False))

    assert codeplane["injected"] == {"skills": [], "memory": []}
