"""T-C02 Change Set 闭环（**真后端接缝**）—— 真实代码文件跑通整条链路。

## 这条用例在防什么

Phase C 的 Change Set 抽屉/右栏是**纯派生**（``frontend/src/views/home/changeSet.ts``），
数据取自 ``GET /runs/{id}``，三动作**复用**既有端点
（``POST /codeplane/runs/{id}/approve|reject``、``POST /runs/{id}/replan``）。因此它是否
能被真实后端的产物喂饱、以及三动作是否真的闭环，**必须由真后端证明**，不能靠桩自证。

本用例在一条**真实** git 仓库上，驱动**完整链路**：

    真实修改（引擎在隔离工作区真的改了 ``mathlib.py`` 这一真实文件）
      → 真实 unified diff（平台自己的 ``tool_handlers._workspace_diff`` = ``git diff``）
      → validation / test（真实 ``codeplane.tests`` 评审证据）
      → HITL（``POST`` 未批准 ⇒ run 落 ``awaiting_approval``，且 ``ApprovalRecord`` pending）
      → approve（真实 ``POST /codeplane/runs/{id}/approve`` ⇒ resume run，``codeplane.committed``
        为真、工作区分支出现真实 ``forgeflow:`` 提交、产出 ``code_diff``/``code_test_report``）
      → reject（真实 ``POST /codeplane/runs/{id}/reject`` ⇒ 工作区被销毁、不留改动）
      → 继续修改（真实 ``POST /runs/{id}/replan`` ⇒ 新 run 句柄）

并把 ``GET /runs/{id}`` 的**真实响应**落盘（``qa_tmp/_a2_pC_T2_backend_payload.json``），
作为前端抽屉桩的**唯一**形状来源（前端 spec 内注明「形状取自真后端」）。

## 被替换的边界（**只有**环境缺失项，不是被测行为）

  * ``forgeflow.codeplane.engine.get_code_engine`` → :class:`_RealEditEngine`：本机无
    OpenHands venv，无法让真引擎（LLM 驱动）改文件。桩**只**做一件真事：在**真实**的
    隔离工作区（``WorkspaceManager`` 真的从种子仓库复制出来的 git 工作树）里，把
    ``mathlib.py`` 真的改写成修正后的内容，再用**平台自己的** ``_workspace_diff``
    （= ``git diff HEAD``）读出**真实** unified diff。即：文件改动与 diff 都是真的，
    只有「谁来改」这一步被脚本化（那正是缺失的引擎边界）。
  * ``forgeflow.codeplane.workspace.get_workspace_manager`` → **真实** ``WorkspaceManager``
    （root 在工程树之外），非桩 —— 工作区、git 提交、销毁都是真的。
  * ``tests`` 评审证据：OpenHands 的测试沙箱本机不可用，故测试结论作为**引擎边界的结构化
    输入**（``TestResult``），与 ``tests/unit/test_inc26_code_approval_projection.py`` 同口径。

## 为什么任务 context 用 ``declared_tools`` + ``repo_path``

``_is_code_task`` 由「显式声明的 ``code.execute``/``code.commit`` 工具」或「``repo_path``
解引用出真实代码源」判定。这里两者都给，使计划里真的出现 ``code.execute`` + ``code.commit``
两步（见 ``orchestrator._candidates_for``），从而自然走到 HITL。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.dependencies import get_current_user
from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.routers import codeplane as codeplane_router
from forgeflow.api.routers import runs as runs_router
from forgeflow.codeplane.tests_verdict import TestResult as _TestResult
from forgeflow.codeplane import engine as engine_module
from forgeflow.codeplane import workspace as workspace_module
from forgeflow.codeplane.workspace import WorkspaceManager
from forgeflow.rbac.models import UserContext
from forgeflow.runtime.orchestrator import (
    RequestContext,
    TaskCreate,
    reset_run_store,
    run_task,
)
from forgeflow.runtime.tool_handlers import _workspace_diff

pytestmark = pytest.mark.asyncio

TENANT = "t-tc02"
APPROVER = "manager-tc02"
INTENT = "修复 mathlib.add 的失败用例"

#: 承重标记：隔离工作区里被真实改动的两行（diff / 提交必须逐字含它）。
_SEED_BODY = "def add(a, b):\n    return a - b\n"
_FIXED_BODY = "def add(a, b):\n    return a + b\n"

_GIT_IDENT = {
    "GIT_AUTHOR_NAME": "ForgeFlow",
    "GIT_AUTHOR_EMAIL": "forgeflow@local",
    "GIT_COMMITTER_NAME": "ForgeFlow",
    "GIT_COMMITTER_EMAIL": "forgeflow@local",
}

#: 前端抽屉桩 payload 的落盘位置（仓库外；真后端证据）。
_PAYLOAD_OUT = Path(r"D:\Agentxm\Multi-Agent\qa_tmp\_a2_pC_T2_backend_payload.json")


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    for key, value in _GIT_IDENT.items():
        env.setdefault(key, value)
    return env


# --------------------------------------------------------------------------- #
# 边界：真实工作区根（工程树之外）+ 真实改动引擎                                  #
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _clean_store():
    reset_run_store()
    yield
    reset_run_store()


@pytest.fixture()
def a_profile(force_memory_backend, monkeypatch):
    """A 档（memory + mock）+ 确定性执行器；不依赖本机 Ollama。"""
    from forgeflow.codeplane.workspace import reset_workspace_manager

    settings = force_memory_backend
    monkeypatch.setattr(settings, "agent_runtime_mode", "deterministic")
    monkeypatch.setattr(settings, "llm_provider", "mock")
    yield settings
    reset_workspace_manager()


@pytest.fixture()
def cp_root():
    """代码面工作区根，**在工程树之外**（``WorkspaceManager`` 会拒绝工程树内的根）。"""
    root = tempfile.mkdtemp(prefix="ff_tc02_")
    yield Path(root)
    shutil.rmtree(root, ignore_errors=True)


class _RealEditEngine:
    """真实改动引擎桩：在真隔离工作区里真的改文件 + 读真 diff（见文件头）。"""

    interpreter = "python"

    def available(self) -> bool:
        return True

    async def run(self, job: object):  # noqa: ANN001
        from forgeflow.codeplane.engine import CodeRunResult

        ws_path = str(getattr(job, "workspace_path", "") or "")
        # 真实改动：把由种子仓库复制出来的真实文件改写成修正后的内容。
        target = Path(ws_path) / "mathlib.py"
        target.write_text(_FIXED_BODY, encoding="utf-8")
        # 真实 diff：走平台自己的 git diff（不是手写字符串）。
        real_diff = _workspace_diff(ws_path)
        return CodeRunResult(
            status="ok",
            diff=real_diff,
            tests=_TestResult(
                passed=3,
                failed=0,
                errors=0,
                verdict="passed",
                measured=True,
                command="pytest -q",
            ),
            timeline=[],
            raw={},
            summary={
                "files_changed": 1,
                "test_command": "pytest -q",
                "passed": 3,
                "failed": 0,
                "repair_rounds": 0,
            },
        )


@pytest.fixture()
def boundaries(cp_root, monkeypatch):
    """只替换环境边界：引擎桩 + **真实** ``WorkspaceManager``（root 在工程树外）。"""
    mgr = WorkspaceManager(root=str(cp_root))
    monkeypatch.setattr(workspace_module, "_MANAGER", mgr, raising=False)
    monkeypatch.setattr(engine_module, "get_code_engine", lambda: _RealEditEngine())
    return mgr


def _seeded_repo(tmp_path: Path) -> Path:
    """一个真实的小型 git 仓库（含基线提交）作为工作区来源。"""
    src = tmp_path / "src"
    src.mkdir()
    (src / "mathlib.py").write_text(_SEED_BODY, encoding="utf-8")
    for argv in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "baseline"],
    ):
        subprocess.run(argv, cwd=str(src), env=_git_env(), capture_output=True, check=False)
    return src


# --------------------------------------------------------------------------- #
# 真后端 API 客户端（真路由 + 真响应模型）                                        #
# --------------------------------------------------------------------------- #
def _client() -> TestClient:
    app = FastAPI()
    app.dependency_overrides[resolve_tenant] = lambda: TENANT
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=APPROVER, role="manager"
    )
    app.include_router(runs_router.router, prefix="/runs")
    app.include_router(codeplane_router.router, prefix="/codeplane")
    return TestClient(app)


async def _run_code_task(repo_path: str):
    """真实 ``run_task`` 落地一次代码任务（真 planner/executor/handler/assembler）。"""
    return await run_task(
        TaskCreate(
            intent=INTENT,
            workflow_type="generic",
            context={
                "declared_tools": ["code.execute", "code.commit"],
                "repo_path": repo_path,
            },
        ),
        RequestContext(tenant_id=TENANT, user_id="engineer-tc02", role="admin"),
    )


async def test_react_cannot_finish_verified_code_without_requesting_review(
    boundaries, a_profile, tmp_path, monkeypatch
):
    from langchain_core.messages import AIMessage
    from forgeflow.runtime import orchestrator as orch

    class Model:
        model = "review-regression"
        _llm_type = "test"
        calls = 0

        def bind_tools(self, tools):
            return self

        def bind(self, **kwargs):
            return self

        async def ainvoke(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return AIMessage(
                    content="", tool_calls=[{"name": "code.execute", "args": {}, "id": "execute"}]
                )
            return AIMessage(content="修复完成。")

    model = Model()
    monkeypatch.setattr(orch.get_settings(), "agent_runtime_mode", "react")
    monkeypatch.setattr(orch, "_build_planner_models", lambda: (model, model, []))
    handle = await _run_code_task(str(_seeded_repo(tmp_path)))
    record = orch.get_run_store().get(handle.run_id)
    assert record.status == "awaiting_approval", record.errors
    assert record.codeplane["approval"]["status"] == "pending"
    assert record.llm["platform_forced_tools"] == ["report.render", "code.commit"]


# --------------------------------------------------------------------------- #
# 1) 真实修改 → diff → test → HITL（原 run 停在 awaiting_approval）             #
# --------------------------------------------------------------------------- #
async def test_real_change_flows_to_hitl_with_real_diff_and_tests(boundaries, a_profile, tmp_path):
    handle = await _run_code_task(str(_seeded_repo(tmp_path)))
    client = _client()

    detail = client.get(f"/runs/{handle.run_id}").json()
    codeplane = detail["codeplane"]

    # 落盘真后端响应，供前端抽屉桩取形状。
    _PAYLOAD_OUT.parent.mkdir(parents=True, exist_ok=True)
    _PAYLOAD_OUT.write_text(json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8")
    print("TC02.run_id =", handle.run_id)
    print("TC02.status =", detail["status"])
    print("TC02.codeplane.diff =\n", codeplane.get("diff"))
    print(
        "TC02.codeplane.tests =",
        json.dumps(codeplane.get("tests"), ensure_ascii=False),
    )
    print(
        "TC02.codeplane.approval =",
        json.dumps(codeplane.get("approval"), ensure_ascii=False),
    )

    # HITL：未批准 ⇒ 运行停 ``awaiting_approval``，且审批为 pending。
    assert detail["status"] == "awaiting_approval"
    assert codeplane.get("approval", {}).get("status") == "pending"

    # 真实 unified diff：含真实改动行，且带标准 git 头。
    diff = codeplane.get("diff") or ""
    assert "diff --git" in diff
    assert "+    return a + b" in diff
    assert "-    return a - b" in diff

    # 真实测试评审证据。
    tests = codeplane.get("tests") or {}
    assert tests.get("measured") is True
    assert tests.get("verdict") == "passed"
    assert tests.get("passed") == 3


# --------------------------------------------------------------------------- #
# 2) approve（真实 POST）→ resume run committed + 真工作区出现真提交 + 产物       #
# --------------------------------------------------------------------------- #
async def test_approve_commits_real_change_and_projects_final_state(
    boundaries, a_profile, tmp_path
):
    handle = await _run_code_task(str(_seeded_repo(tmp_path)))
    client = _client()

    before = client.get(f"/runs/{handle.run_id}").json()
    assert before["status"] == "awaiting_approval"
    original_ws = (before["codeplane"].get("workspace") or {}).get("workspace_id")
    assert original_ws

    resp = client.post(f"/codeplane/runs/{handle.run_id}/approve", json={"note": "QA approve"})
    assert resp.status_code == 200, resp.text
    resume_id = resp.json()["run_id"]
    assert resume_id and resume_id != handle.run_id

    # 原 run：approval 回写 approved（final state 之一）。
    original = client.get(f"/runs/{handle.run_id}").json()
    approval = original["codeplane"].get("approval") or {}
    print("TC02.approve.original.approval =", json.dumps(approval, ensure_ascii=False))
    assert approval.get("status") == "approved"

    # resume run：真实提交 ⇒ codeplane.committed == True（``changeSet.applied`` 的真源）。
    resume = client.get(f"/runs/{resume_id}").json()
    resume_cp = resume["codeplane"]
    print("TC02.approve.resume.committed =", resume_cp.get("committed"))
    assert resume_cp.get("committed") is True

    # resume run 携带代码产物（diff + 测试报告）。
    kinds = [a.get("kind") for a in (resume.get("artifacts") or [])]
    print("TC02.approve.resume.artifact_kinds =", kinds)
    assert "code_diff" in kinds
    assert "code_test_report" in kinds

    # 真工作区里出现真实的 ``forgeflow:`` 提交（不是桩）。
    ws = boundaries.get(original_ws)
    assert ws is not None and ws.path
    log = subprocess.run(
        ["git", "-C", ws.path, "log", "--format=%s"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_git_env(),
        check=False,
    )
    subjects = [s.strip() for s in (log.stdout or "").splitlines()]
    print("TC02.approve.workspace.git_log =", subjects)
    assert any(s.startswith("forgeflow: ") for s in subjects)


# --------------------------------------------------------------------------- #
# 3) reject（真实 POST）→ 工作区销毁、不留改动、approval rejected              #
# --------------------------------------------------------------------------- #
async def test_reject_destroys_workspace_and_leaves_no_change(boundaries, a_profile, tmp_path):
    handle = await _run_code_task(str(_seeded_repo(tmp_path)))
    client = _client()

    before = client.get(f"/runs/{handle.run_id}").json()
    ws_id = (before["codeplane"].get("workspace") or {}).get("workspace_id")
    assert ws_id

    resp = client.post(f"/codeplane/runs/{handle.run_id}/reject", json={"note": "QA reject"})
    assert resp.status_code == 200, resp.text
    # 拒绝复用同一个 run（不产生 resume run）。
    assert resp.json()["run_id"] == handle.run_id

    original = client.get(f"/runs/{handle.run_id}").json()
    approval = original["codeplane"].get("approval") or {}
    print("TC02.reject.approval =", json.dumps(approval, ensure_ascii=False))
    assert approval.get("status") == "rejected"

    # 工作区被销毁：不留改动。
    destroyed = boundaries.get(ws_id)
    assert destroyed is not None and destroyed.state == "destroyed"

    # 未提交：无代码产物。
    kinds = [a.get("kind") for a in (original.get("artifacts") or [])]
    assert "code_diff" not in kinds


# --------------------------------------------------------------------------- #
# 4) 继续修改（真实 POST /runs/{id}/replan）→ 新 run 句柄                      #
# --------------------------------------------------------------------------- #
# 诚实边界（**非** Phase C 缺陷，如实记录）：前端「继续修改」**复用**既有
# ``POST /runs/{id}/replan``，其契约是「按原 intent/声明重跑一次」。本用例的原 run
# 之所以是代码任务，是靠 ``declared_tools`` 里的 ``code.execute``/``code.commit``
# 声明；而 ``declared_tools`` **不是** ``orchestrator._EXPLICIT_INPUT_KEYS`` 之一，
# 故 ``_declared_inputs`` 不会把它写进 ``record.declared_inputs`` ⇒ replan 的 context
# 只剩 ``repo_path``，而 ``_resolve_resource_inputs`` 只解引用 ``resources``（不 passthrough
# 裸 ``repo_path``）⇒ replan 落回默认步骤集。这是既有平台行为，Phase C 不动后端。
# 因此本用例只钉「replan 真的产出了**新** run（真路由 + 真 run_task）」，不臆称新 run 仍是代码面。
async def test_reanalyze_replans_through_real_route(boundaries, a_profile, tmp_path):
    handle = await _run_code_task(str(_seeded_repo(tmp_path)))
    client = _client()

    resp = client.post(f"/runs/{handle.run_id}/replan", json={"reason": "继续修改"})
    assert resp.status_code == 200, resp.text
    new_id = resp.json()["run_id"]
    print("TC02.replan.from =", handle.run_id, "to =", new_id)
    assert new_id and new_id != handle.run_id

    # replan 真的落地了**新** run（真路由 + 真 run_task）。
    new_detail = client.get(f"/runs/{new_id}").json()
    print("TC02.replan.new.status =", new_detail["status"])
    print(
        "TC02.replan.new.step_tools =",
        [s.get("tool") for s in (new_detail.get("steps") or [])],
    )
    assert new_detail["run_id"] == new_id
