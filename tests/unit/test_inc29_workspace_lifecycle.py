"""INC29 T01 —— §7 工作区生命周期收口 + TTL reaper（消灭死配置 + 磁盘泄漏）。

## 留存经验（为什么要这条钉子）

``WorkspaceManager`` 的「保留供取证」（``destroy=False``）语义本意是对的，但两个洞
把它变成**磁盘泄漏**：

1. 「任务完成 → 回收」此前**只接在 reject 上**
   （``api/routers/codeplane.py::reject_code_run``）；**批准成功**（提交）后工作区
   **从不回收**（``runtime/orchestrator.py::run_task`` 的终止态无回收）；
2. ``config.py::Settings.codeplane_workspace_ttl_hours`` 自述「advisory … never a
   silent background delete」，而全仓**没有任何消费者** —— 一个死配置。

修法（INC29 §4.A）：

* ``WorkspaceManager.release_for_run`` —— 按 run 回收其 live 工作区（无 ⇒ ``None``）；
* ``WorkspaceManager.reap_expired`` —— 读 TTL，**仅当 ttl>0 才真删**超期工作区，
  写 ``reaped`` 台账；``ttl<=0`` 时**一个不删**并返回 ``[]``；
* ``orchestrator.run_task`` 在**终止态**回收（成功且已提交 ⇒ 保留；失败/终止 ⇒ 销毁）。

引文一律 ``file.py::symbol``，不用行号。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from forgeflow.api.routers.codeplane import CodeDecisionRequest, approve_code_run
from forgeflow.codeplane import workspace as ws_mod
from forgeflow.codeplane.approval import request_code_approval
from forgeflow.codeplane.workspace import WorkspaceManager
from forgeflow.config import get_settings
from forgeflow.rbac.models import UserContext
from forgeflow.repositories import get_policy_repository
from forgeflow.repositories.base import utcnow
from forgeflow.runtime.orchestrator import RunRecord, get_run_store, reset_run_store

_TENANT = "tenant-t01-lifecycle"
_APPROVER = "manager-t01"

_GIT_IDENT = {
    "GIT_AUTHOR_NAME": "ForgeFlow",
    "GIT_AUTHOR_EMAIL": "forgeflow@local",
    "GIT_COMMITTER_NAME": "ForgeFlow",
    "GIT_COMMITTER_EMAIL": "forgeflow@local",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _real_deletes_available() -> bool:
    """Whether ``shutil.rmtree`` really deletes on this host.

    The WorkBuddy host wraps ``shutil.rmtree`` with a safe-delete shim
    (``shutil.rmtree.__name__ == "_safe_shutil_rmtree"``) that routes deletions
    to a trash/refuse path, so a *physical* directory may survive ``destroy=True``
    even though the workspace is correctly marked ``destroyed``. The state /
    ledger contract is asserted unconditionally; the on-disk assertion is only
    meaningful when real deletes happen (run with ``CODEBUDDY_SAFE_DELETE_ENABLED=0``
    to enforce it).
    """
    return getattr(shutil.rmtree, "__name__", "rmtree") == "rmtree"


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    for key, value in _GIT_IDENT.items():
        env.setdefault(key, value)
    return env


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #
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
    """A workspace root **outside** the project tree (the manager rejects an inside one)."""
    root = tempfile.mkdtemp(prefix="ff_codeplane_t01_")
    yield Path(root)
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture()
def mgr(cp_root, monkeypatch):
    """A live manager rooted outside the project tree, installed process-wide."""
    manager = WorkspaceManager(root=str(cp_root))
    monkeypatch.setattr(ws_mod, "_MANAGER", manager, raising=False)
    return manager


# --------------------------------------------------------------------------- #
# TTL reaper                                                                   #
# --------------------------------------------------------------------------- #
def test_reap_expired_destroys_workspaces_past_the_ttl(mgr, monkeypatch):
    monkeypatch.setattr(get_settings(), "codeplane_workspace_ttl_hours", 24)
    ws = mgr.create("run-old")
    path = Path(ws.path)
    assert path.exists()
    ws.created_at = (utcnow() - timedelta(hours=48)).isoformat()  # 48h > 24h TTL

    reaped = mgr.reap_expired()

    assert reaped == [ws.workspace_id]
    assert ws.state == "destroyed"
    assert mgr.reuse("run-old") is None, "被回收的工作区仍被视为 live"
    assert any(e["event"] == "reaped" for e in ws.events)
    assert any(e["event"] == "reaped" for e in mgr.history())
    if _real_deletes_available():
        assert not path.exists(), "到期工作区未被真删（磁盘仍在泄漏）"


def test_reap_expired_keeps_workspaces_inside_the_ttl(mgr, monkeypatch):
    monkeypatch.setattr(get_settings(), "codeplane_workspace_ttl_hours", 24)
    ws = mgr.create("run-fresh")  # created_at == now ⇒ inside the TTL

    assert mgr.reap_expired() == []
    assert ws.state != "destroyed"
    assert Path(ws.path).exists()


def test_reap_expired_is_a_noop_when_ttl_is_zero(mgr, monkeypatch):
    monkeypatch.setattr(get_settings(), "codeplane_workspace_ttl_hours", 0)
    ws = mgr.create("run-keep")
    ws.created_at = (utcnow() - timedelta(hours=999)).isoformat()
    path = Path(ws.path)

    reaped = mgr.reap_expired()

    assert reaped == [], "ttl<=0 必须一个都不删"
    assert ws.state != "destroyed"
    assert path.exists(), "ttl<=0 时工作区被误删"


def test_create_sweeps_expired_workspaces(mgr, monkeypatch):
    """The TTL setting now has a real consumer: ``create`` sweeps before it makes one."""
    monkeypatch.setattr(get_settings(), "codeplane_workspace_ttl_hours", 12)
    old = mgr.create("run-x")
    old.created_at = (utcnow() - timedelta(hours=24)).isoformat()

    mgr.create("run-y")  # WorkspaceManager.create reaps expired workspaces first

    assert old.state == "destroyed"


# --------------------------------------------------------------------------- #
# release_for_run                                                              #
# --------------------------------------------------------------------------- #
def test_release_for_run_returns_none_without_a_workspace(mgr):
    assert mgr.release_for_run("no-such-run") is None
    assert mgr.release_for_run("no-such-run", destroy=True) is None


def test_release_for_run_releases_and_keeps_files(mgr):
    ws = mgr.create("run-a")
    released = mgr.release_for_run("run-a", destroy=False)

    assert released is not None and released.workspace_id == ws.workspace_id
    assert (mgr.describe("run-a") or {}).get("state") == "released"
    assert Path(ws.path).exists(), "destroy=False 必须保留文件供取证"


def test_release_for_run_can_destroy(mgr):
    ws = mgr.create("run-b")
    mgr.release_for_run("run-b", destroy=True)

    assert (mgr.describe("run-b") or {}).get("state") == "destroyed"
    assert mgr.reuse("run-b") is None
    if _real_deletes_available():
        assert not Path(ws.path).exists()


# --------------------------------------------------------------------------- #
# 终态回收（run_task）—— 验收标准：「批准并提交成功」的运行结束后 state ∈ {released, destroyed}
# --------------------------------------------------------------------------- #
def _seeded_repo(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "mathlib.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    for argv in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "baseline"],
    ):
        subprocess.run(argv, cwd=str(src), env=_git_env(), capture_output=True, check=False)
    return src


def _workspace_with_pending_change(cp_root, tmp_path, monkeypatch, run_id):
    manager = WorkspaceManager(root=str(cp_root))
    monkeypatch.setattr(ws_mod, "_MANAGER", manager, raising=False)
    ws = manager.create(run_id, {"path": str(_seeded_repo(tmp_path))})
    (Path(ws.path) / "mathlib.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    return manager, ws


def _awaiting_run_record(run_id: str, ws) -> RunRecord:
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
            "tests": {"measured": True, "passed": 3, "failed": 0, "verdict": "passed"},
            "diff": "",
        },
    )


async def test_approved_and_committed_run_releases_its_workspace(
    tmp_path, cp_root, monkeypatch, a_profile
):
    """验收标准：批准并提交成功后，该 run 的工作区 describe().state ∈ {released, destroyed}。"""
    code_run_id = "run-code-t01-approve"
    manager, ws = _workspace_with_pending_change(cp_root, tmp_path, monkeypatch, code_run_id)
    get_run_store().save(_awaiting_run_record(code_run_id, ws))
    await request_code_approval(
        get_policy_repository(),
        tenant_id=_TENANT,
        run_id=code_run_id,
        requester="engineer-1",
        note="修复 mathlib.add 的失败测试",
    )

    # 前置：工作区确为 live（否则断言会在空集上「空转通过」）。
    assert (manager.describe(code_run_id) or {}).get("state") == "active"

    await approve_code_run(
        run_id=code_run_id,
        request=CodeDecisionRequest(note="QA approve"),
        user=UserContext(user_id=_APPROVER, role="manager"),
        tenant=_TENANT,
    )

    after = manager.describe(code_run_id) or {}
    assert after.get("state") in {"released", "destroyed"}, f"终态未回收：{after}"
    assert after.get("state") != "active"
