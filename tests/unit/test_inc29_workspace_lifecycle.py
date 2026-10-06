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
import stat
import subprocess
import tempfile
import time
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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

#: INC51 fix — captured at import, *before* any test injects a fake
#: ``shutil.rmtree``. C/D/E monkeypatch ``ws_mod.shutil.rmtree`` (the global
#: ``shutil`` module!) to force a destroy failure; that must never be able to
#: sabotage the ``cp_root`` fixture's own teardown, so teardown deletes through
#: this captured real deleter, never through a call-time ``shutil.rmtree`` lookup.
_REAL_RMTREE = shutil.rmtree

_GIT_IDENT = {
    "GIT_AUTHOR_NAME": "ForgeFlow",
    "GIT_AUTHOR_EMAIL": "forgeflow@local",
    "GIT_COMMITTER_NAME": "ForgeFlow",
    "GIT_COMMITTER_EMAIL": "forgeflow@local",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _real_deletes_available() -> bool:
    """Whether ``shutil.rmtree`` really deletes on this host (explicit capability).

    The WorkBuddy host wraps ``shutil.rmtree`` with a safe-delete shim
    (``shutil.rmtree.__name__ == "_safe_shutil_rmtree"``) that routes deletions
    to a trash/refuse path, so a *physical* directory may survive a delete even
    though the workspace's state contract is honoured. INC51 T02 makes this an
    **explicit, visible capability declaration** — ``tests/conftest.py::
    pytest_report_header`` always prints ``REAL_DELETE_AVAILABLE=true|false`` in
    the run header — never a silent escape hatch: tests that need to verify a
    *physical* deletion call :func:`_require_real_deletes`, which **skips with a
    loud reason** and never reports a green PASS it did not verify.
    """
    return getattr(shutil.rmtree, "__name__", "rmtree") == "rmtree"


def _require_real_deletes() -> None:
    """Skip (loudly) the *physical-deletion* assertion when the shim is active."""
    if not _real_deletes_available():
        pytest.skip("REAL_DELETE_AVAILABLE=false — 本机无法验证物理删除，测试受环境限制")


def _cleanup_tree(
    path: str,
    rmtree: Any,
    *,
    attempts: int = 3,
    backoff: float = 0.15,
) -> tuple[bool, str]:
    """Bounded, verified removal through an **explicit** ``rmtree`` callable.

    This mirrors the production ``_remove_tree`` semantics (bounded retries,
    ``onexc`` handler that clears the read-only bit then retries ``func``, and an
    ``os.path.exists`` verification each pass) but takes the delete callable as a
    parameter. That is the whole point: the ``cp_root`` fixture passes the
    *captured real* deleter, so a test that monkeypatches ``shutil.rmtree``
    (C/D/E inject a failure) cannot sabotage the fixture's own teardown.

    Returns ``(removed, last_error)``; ``removed`` is ``True`` only when the path
    is confirmed gone.
    """
    target = Path(path)
    errors: list[str] = []
    last_error = ""

    def _on_error(func: Any, sub: str, exc: BaseException) -> None:
        errors.append(f"{func}({sub}) :: {type(exc).__name__}: {exc}")
        try:
            os.chmod(sub, stat.S_IWRITE)
        except OSError as chmod_exc:
            errors.append(f"chmod({sub}) :: {type(chmod_exc).__name__}: {chmod_exc}")
            return
        try:
            func(sub)
        except Exception as retry_exc:  # noqa: BLE001 — still-unrecoverable entry
            errors.append(f"{func}({sub}) retry :: {type(retry_exc).__name__}: {retry_exc}")

    for attempt in range(1, attempts + 1):
        try:
            rmtree(str(target), onexc=_on_error)
        except Exception as exc:  # noqa: BLE001 — rmtree can raise on the root
            last_error = f"{type(exc).__name__}: {exc}"
        if not os.path.exists(target):
            return True, ""
        if not last_error and errors:
            last_error = errors[-1]
        if not last_error:
            last_error = "删除调用返回但目录仍然存在"
        if attempt < attempts:
            time.sleep(backoff * attempt)
    return False, last_error


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
    """A workspace root **outside** the project tree (the manager rejects an inside one).

    Teardown deletes through the **captured real** deleter (snapshotted in setup,
    immune to any later ``shutil.rmtree`` monkeypatch) via the bounded, verified
    :func:`_cleanup_tree` — never the old ``shutil.rmtree(..., ignore_errors=True)``
    and never a call-time ``shutil.rmtree`` lookup. This fixture is the direct
    producer of the ``D:\\Temp\\ff_codeplane_t01_*`` leftovers; the old silent
    ``ignore_errors=True`` leaked a directory on every failed delete. A residual
    directory is now *warned about* (never silently swallowed).
    """
    root = tempfile.mkdtemp(prefix="ff_codeplane_t01_")
    # Snapshot at setup: C/D/E inject a failing ``shutil.rmtree`` *after* this,
    # so this snapshot is guaranteed to be the real deleter (the reported
    # teardown failures under a call-time lookup proved the patch outlives the
    # test body — we must not depend on teardown ordering).
    real_rmtree = shutil.rmtree
    yield Path(root)
    removed, last_error = _cleanup_tree(str(root), real_rmtree)
    if not removed:
        warnings.warn(
            f"cp_root 残留未清理：{root}（{last_error}）；请检查瞬时锁/句柄占用",
            stacklevel=1,
        )


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
    _require_real_deletes()
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
    _require_real_deletes()
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
    _require_real_deletes()
    ws = mgr.create("run-b")
    mgr.release_for_run("run-b", destroy=True)

    assert (mgr.describe("run-b") or {}).get("state") == "destroyed"
    assert mgr.reuse("run-b") is None
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


# --------------------------------------------------------------------------- #
# INC51 T02 — 诚实销毁（A–E）。状态必须反映事实：``destroyed`` ⇔ 路径已确认不存在。
# --------------------------------------------------------------------------- #
def test_inc51_A_destroy_confirms_the_path_is_really_gone(mgr):
    """A — 正常 destroy：``state == "destroyed"`` 且 path 确实不存在。"""
    _require_real_deletes()
    ws = mgr.create("run-51a")
    path = Path(ws.path)
    assert path.exists()

    mgr.release(ws.workspace_id, destroy=True)

    assert ws.state == "destroyed", f"正常销毁后状态应 destroyed，实际 {ws.state}"
    assert not path.exists(), "destroyed 但路径仍在（假台账 / 磁盘泄漏）"


def test_inc51_B_destroy_succeeds_after_a_transient_failure(mgr, monkeypatch):
    """B — 第 1 次删除失败、第 2 次成功 ⇒ ``destroyed`` 且 path 不存在（有界重试）。"""
    _require_real_deletes()
    ws = mgr.create("run-51b")
    path = Path(ws.path)
    real_rmtree = shutil.rmtree
    calls = {"n": 0}

    def flaky(target, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("transient lock (INC51-B)")
        return real_rmtree(target, *args, **kwargs)

    monkeypatch.setattr(ws_mod.shutil, "rmtree", flaky)
    mgr.release(ws.workspace_id, destroy=True)

    assert calls["n"] >= 2, "首次失败后未重试"
    assert ws.state == "destroyed", f"重试成功后应 destroyed，实际 {ws.state}"
    assert not path.exists(), "重试成功后路径仍在"


def test_inc51_C_failure_is_reported_honestly(mgr, monkeypatch):
    """C — 连续全部失败 ⇒ ``destroy_failed``、path 仍在、台账事件字段齐全。"""
    ws = mgr.create("run-51c")
    path = Path(ws.path)

    def always_fail(target, *args, **kwargs):
        raise PermissionError("locked (INC51-C)")

    monkeypatch.setattr(ws_mod.shutil, "rmtree", always_fail)
    mgr.release(ws.workspace_id, destroy=True)

    assert ws.state == "destroy_failed", f"全部失败后应 destroy_failed，实际 {ws.state}"
    assert path.exists(), "destroy_failed 时路径不应消失"
    events = [e for e in ws.events if e["event"] == "destroy_failed"]
    assert events, "缺少 destroy_failed 台账事件"
    ev = events[-1]
    for key in (
        "workspace_id",
        "state",
        "path",
        "attempt_count",
        "failure_reason",
        "timestamp",
    ):
        assert key in ev, f"destroy_failed 事件缺字段：{key}"
    assert ev["state"] == "destroy_failed"
    assert ev["workspace_id"] == ws.workspace_id
    assert ev["path"] == ws.path
    assert ev["attempt_count"] == 3, "attempt_count 应为有界重试上限 3"
    assert ev["failure_reason"], "failure_reason 不应为空"
    assert any(e["event"] == "destroy_failed" for e in mgr.history())

    # INC51 fix — close the injection window explicitly: the fake deleter must
    # only cover the assertion window, never the fixture teardown.
    monkeypatch.setattr(ws_mod.shutil, "rmtree", _REAL_RMTREE)


def test_inc51_D_silent_noop_delete_is_a_failure(mgr, monkeypatch):
    """D — 删除 API 不抛异常但目录仍在 ⇒ 必须 ``destroy_failed``（不许当成功）。"""
    ws = mgr.create("run-51d")
    path = Path(ws.path)

    def noop(target, *args, **kwargs):
        return None  # 不删除、也不抛异常

    monkeypatch.setattr(ws_mod.shutil, "rmtree", noop)
    mgr.release(ws.workspace_id, destroy=True)

    assert ws.state == "destroy_failed", f"静默 no-op 应判失败，实际 {ws.state}"
    assert path.exists(), "路径仍在但被当成成功"

    monkeypatch.setattr(ws_mod.shutil, "rmtree", _REAL_RMTREE)


def test_inc51_E_destroy_failed_is_not_reusable(mgr, monkeypatch):
    """E — ``destroy_failed`` 状态下 ``reuse()`` 必须返回 ``None``。"""

    def always_fail(target, *args, **kwargs):
        raise PermissionError("locked (INC51-E)")

    monkeypatch.setattr(ws_mod.shutil, "rmtree", always_fail)
    ws = mgr.create("run-51e")
    mgr.release(ws.workspace_id, destroy=True)

    assert ws.state == "destroy_failed"
    assert mgr.reuse("run-51e") is None, "destroy_failed 的工作区不得被 reuse 当成 live"

    monkeypatch.setattr(ws_mod.shutil, "rmtree", _REAL_RMTREE)
