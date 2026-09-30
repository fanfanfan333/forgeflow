"""INC26 收口钉子 —— AC-12 审批闭环的**双 run 投影**不变式。

## 为什么会有这条钉子（留存的经验）

本轮 AC-12 首轮被判「未通过」，根因**不是源码缺陷，而是该行为原先没有任何自动化
测试钉住**：审批闭环（原 run 回写 approval + 产物落在 **resume run**）只靠手工调
API 验证，套件零覆盖。于是「读错对象」（把产物找在原 run 上）能一路活到验收报告。
本条钉子把这条不变式**显式写进测试**，从此任何人改动
``forgeflow/api/routers/codeplane.py::approve_code_run`` /
``forgeflow/api/routers/codeplane.py::_reflect_decision`` 都会立刻见红。

## 钉死的三条机制（全部落在**两个不同的 run** 上）

1. **原 run 审批回写**：批准后，**原 run** 的 ``codeplane.approval.status == "approved"``，
   且 ``decided_by`` / ``decided_at`` 非空（``_reflect_decision`` 的产物）。
2. **产物归属复跑**：``approve`` 返回的 ``run_id`` **≠** 原 ``run_id``，且该 resume run 的
   ``artifacts`` 至少各含一条 ``code_diff`` 与 ``code_test_report``（
   ``runtime/orchestrator.py::run_task`` + ``runtime/artifacts.py::artifacts_from_invocations``
   的产物）。
3. **反双重归属**：原 run 的 ``artifacts`` **不**含任何代码产物 —— 把「产物只归复跑」
   这条不变式显式钉死，防止将来有人把产物复制回原 run（两个 run 各自声称拥有同一次
   提交，违反 L1/L2 同源口径）。

外加一条 reject 对照：拒绝后原 run ``codeplane.approval.status == "rejected"``，
且工作区被销毁（不留改动，AC-21）。

## 档位与引文纪律

* 依赖存储后端 ⇒ 显式声明 **A 档**（``force_memory_backend``，即 memory）并钉死
  ``agent_runtime_mode="deterministic"`` + ``llm_provider="mock"``：**不依赖本机 Ollama**。
  resume run 的 ``code.execute`` 走「审批复跑」分支（复用工作区、**不**重跑引擎），
  故引擎可用性对本条用例无影响。
* 引文一律 ``file.py::symbol``，不用行号。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from forgeflow.api.routers.codeplane import CodeDecisionRequest, approve_code_run, reject_code_run
from forgeflow.codeplane import workspace as ws_mod
from forgeflow.codeplane.approval import find_code_approval, request_code_approval
from forgeflow.codeplane.workspace import WorkspaceManager
from forgeflow.rbac.models import UserContext
from forgeflow.repositories import get_policy_repository
from forgeflow.runtime.orchestrator import RunRecord, get_run_store, reset_run_store

#: 承重标记：工作区里被改动的行（commit 后 code_diff 必须逐字含它）。
_FIXED_BODY = "def add(a, b):\n    return a + b\n"
_SEED_BODY = "def add(a, b):\n    return a - b\n"

_GIT_IDENT = {
    "GIT_AUTHOR_NAME": "ForgeFlow",
    "GIT_AUTHOR_EMAIL": "forgeflow@local",
    "GIT_COMMITTER_NAME": "ForgeFlow",
    "GIT_COMMITTER_EMAIL": "forgeflow@local",
}

_TENANT = "tenant-approval-nail"
_APPROVER = "manager-approval-nail"
_CODE_TEST_EVIDENCE = {
    "measured": True,
    "passed": 3,
    "failed": 0,
    "errors": 0,
    "verdict": "passed",
    "command": "pytest -q",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    for key, value in _GIT_IDENT.items():
        env.setdefault(key, value)
    return env


@pytest.fixture()
def a_profile(force_memory_backend, monkeypatch):
    """A 档（memory + mock）+ 确定性执行器；不依赖 Ollama（显式声明档位）。"""
    from forgeflow.codeplane.workspace import reset_workspace_manager

    settings = force_memory_backend
    monkeypatch.setattr(settings, "agent_runtime_mode", "deterministic")
    monkeypatch.setattr(settings, "llm_provider", "mock")
    reset_run_store()
    yield settings
    reset_run_store()
    reset_workspace_manager()


@pytest.fixture()
def cp_root():
    """A code-plane workspace root **outside** the project tree (AC-11 guard).

    Built from the system temp dir — never from pytest's ``tmp_path``/``--basetemp``,
    which a CI may point *inside* the checkout (the product's
    ``WorkspaceManager.__init__`` correctly rejects a root under the project tree,
    so the nail must not depend on where pytest writes its temp files).
    """
    root = tempfile.mkdtemp(prefix="ff_codeplane_")
    yield Path(root)
    shutil.rmtree(root, ignore_errors=True)


def _seeded_repo(tmp_path: Path) -> Path:
    """A tiny git repo with a baseline commit (the workspace source)."""
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


def _workspace_with_pending_change(
    cp_root: Path, tmp_path: Path, monkeypatch, run_id: str
) -> tuple[WorkspaceManager, object]:
    """A live workspace (rooted outside the project tree) holding an **uncommitted** change."""
    mgr = WorkspaceManager(root=str(cp_root))
    monkeypatch.setattr(ws_mod, "_MANAGER", mgr, raising=False)
    ws = mgr.create(run_id, {"path": str(_seeded_repo(tmp_path))})
    (Path(ws.path) / "mathlib.py").write_text(_FIXED_BODY, encoding="utf-8")
    return mgr, ws


def _awaiting_run_record(run_id: str, ws) -> RunRecord:
    """A code-plane run that reached ``awaiting_approval`` (preconditions of approve/reject)."""
    return RunRecord(
        run_id=run_id,
        thread_id=f"thread-{run_id}",
        tenant_id=_TENANT,
        agent_id=None,
        intent="修复 mathlib.add 的失败测试",
        status="awaiting_approval",
        outcome="awaiting_approval",
        steps=[{"tool": "code.execute", "status": "ok", "note": "代码执行面"}],
        errors=[],
        created_at=_now(),
        codeplane={
            "engine": {"available": True, "interpreter": "python"},
            "degraded": None,
            "workspace": ws.to_dict(),
            "timeline": [],
            "tests": dict(_CODE_TEST_EVIDENCE),
            "diff": "",
        },
    )


def _kinds(record) -> list[str]:
    return [str(a.get("kind") or "") for a in (getattr(record, "artifacts", None) or [])]


# --------------------------------------------------------------------------- #
# 主断言 —— 批准：原 run 回写 + 产物落在 resume run                            #
# --------------------------------------------------------------------------- #
async def test_approve_projects_to_original_run_and_artifacts_to_resume_run(
    tmp_path, cp_root, monkeypatch, a_profile
):
    code_run_id = "run-code-approve-1"
    _mgr, ws = _workspace_with_pending_change(cp_root, tmp_path, monkeypatch, code_run_id)
    get_run_store().save(_awaiting_run_record(code_run_id, ws))
    await request_code_approval(
        get_policy_repository(),
        tenant_id=_TENANT,
        run_id=code_run_id,
        requester="engineer-1",
        note="修复 mathlib.add 的失败测试",
    )

    # --- 前置：原 run 确为 awaiting_approval、无产物、且存在 pending 审批 -------- #
    # （防止断言在空集上「空转通过」。）
    before = get_run_store().get(code_run_id)
    assert before.status == "awaiting_approval"
    assert _kinds(before) == []
    pending = await find_code_approval(get_policy_repository(), _TENANT, code_run_id)
    assert pending is not None and pending.status == "pending"

    resp = await approve_code_run(
        run_id=code_run_id,
        request=CodeDecisionRequest(note="QA approve"),
        user=UserContext(user_id=_APPROVER, role="manager"),
        tenant=_TENANT,
    )

    # --- 断言 2：resume run 是**另一个** run，且携带代码产物 ----------------- #
    assert resp.run_id and resp.run_id != code_run_id
    resume = get_run_store().get(resp.run_id)
    assert resume is not None, "approve 未落地 resume run"
    resume_kinds = _kinds(resume)
    assert "code_diff" in resume_kinds, f"resume run 缺少 code_diff：{resume_kinds}"
    assert "code_test_report" in resume_kinds, f"resume run 缺少 code_test_report：{resume_kinds}"
    diff_artifact = next(a for a in resume.artifacts if a["kind"] == "code_diff")
    assert "return a + b" in diff_artifact["content"], "code_diff 不是本次真实变更"

    # --- 断言 1：原 run 的 approval 被回写（含 decided_by / decided_at） ------- #
    original = get_run_store().get(code_run_id)
    approval = (original.codeplane or {}).get("approval") or {}
    assert approval.get("status") == "approved", f"原 run approval 未回写：{approval}"
    assert approval.get("decided_by") == _APPROVER
    assert approval.get("decided_at"), "decided_at 为空"

    # --- 断言 3：原 run **不**得含代码产物（反双重归属） ---------------------- #
    assert "code_diff" not in _kinds(original)
    assert "code_test_report" not in _kinds(original)


# --------------------------------------------------------------------------- #
# reject 对照 —— 原 run 回写 rejected，且工作区被销毁（不留改动，AC-21）        #
# --------------------------------------------------------------------------- #
async def test_reject_projects_to_original_run_and_leaves_no_change(
    tmp_path, cp_root, monkeypatch, a_profile
):
    code_run_id = "run-code-reject-1"
    mgr, ws = _workspace_with_pending_change(cp_root, tmp_path, monkeypatch, code_run_id)
    ws_id = ws.workspace_id
    get_run_store().save(_awaiting_run_record(code_run_id, ws))
    await request_code_approval(
        get_policy_repository(),
        tenant_id=_TENANT,
        run_id=code_run_id,
        requester="engineer-1",
        note="修复 mathlib.add 的失败测试",
    )

    resp = await reject_code_run(
        run_id=code_run_id,
        request=CodeDecisionRequest(note="QA reject"),
        user=UserContext(user_id=_APPROVER, role="manager"),
        tenant=_TENANT,
    )

    # 拒绝复用同一个 run（不产生 resume run）。
    assert resp.run_id == code_run_id

    original = get_run_store().get(code_run_id)
    approval = (original.codeplane or {}).get("approval") or {}
    assert approval.get("status") == "rejected", f"原 run approval 未回写：{approval}"
    assert approval.get("decided_by") == _APPROVER
    assert approval.get("decided_at"), "decided_at 为空"
    # 无代码产物（未提交）。
    assert "code_diff" not in _kinds(original)
    assert "code_test_report" not in _kinds(original)
    # 工作区被销毁：不留改动。
    destroyed = mgr.get(ws_id)
    assert destroyed is not None and destroyed.state == "destroyed"
