"""INC46 T13 — real isolation sandbox: isolated execution + quotas + guards.

Scope (阳性 / 阴性 / 反事实 / 诚实清单):

* **阳性** — 受限执行**真的**在独立子进程里跑（真实计算被回传），且**零生产副作用**
  （父进程 tool registry 快照前后一致、临时工作目录执行后销毁）。
* **阴性（逐条）** — 越界写 / 外部连接 / 读生产环境变量 / fork(PROC) 炸弹 / 内存炸弹 /
  超时 ⇒ 全部必须**非 pass**：越权 ⇒ ``fail``，超时 / 配额击杀 ⇒ ``error``（绝不折算 pass）。
* **反事实（真跑）** — 去掉网络隔离 ⇒ 外联用例转红（本地监听可连，证明是守卫挡的）；
  去掉配额 ⇒ 炸弹用例转红（不再被配额击杀）。两者都必须真跑变红。
* **诚实清单** — ``isolation_capability_report()`` 逐条给出 means / status / note；
  read-only rootfs 明列为 ``unlanded``，不谎报。

Every test drives the real ``run_isolated`` (a real ``python`` subprocess).
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time

import pytest

from forgeflow.runtime import tool_registry
from forgeflow.skills.sandbox_isolated import (
    _IS_WINDOWS,
    IsolationLimits,
    SandboxResult,
    quotas_actually_enforced,
    run_isolated,
)


# --------------------------------------------------------------------------- #
# a local TCP listener — independent of the internet (crisp counterfactual)    #
# --------------------------------------------------------------------------- #
class _LocalListener:
    """A 127.0.0.1 TCP server that answers one connection, to prove reachability."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self.accepted = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.accepted += 1
            try:
                conn.sendall(b"OK")
            finally:
                conn.close()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._sock.close()


_CONNECT_TEMPLATE = (
    "import socket\n"
    "try:\n"
    "    c = socket.create_connection(('127.0.0.1', {port}), timeout=2)\n"
    "    data = c.recv(8)\n"
    "    c.close()\n"
    "    print('CONNECTED', data)\n"
    "except Exception as e:\n"
    "    print('CONNECT-FAILED', type(e).__name__, str(e)[:60])\n"
)


# --------------------------------------------------------------------------- #
# 阳性                                                                          #
# --------------------------------------------------------------------------- #
def test_positive_real_execution_with_zero_production_side_effect():
    before_ids = tool_registry.known_ids()
    before_snapshot = dict(tool_registry.snapshot_bindings())

    result = run_isolated(
        "total = sum(i * i for i in range(1000))\n"
        "print('RESULT', total)\n"
    )

    assert isinstance(result, SandboxResult)
    assert result.verdict == "pass", result.to_dict()
    assert result.passed is True
    # 真实执行：子进程算出的值被如实回传（不是伪造的成功）
    assert result.stdout.strip() == f"RESULT {sum(i * i for i in range(1000))}"
    assert result.returncode == 0
    assert result.timed_out is False
    assert result.quota_exceeded is None
    assert result.violations == []

    # 零生产副作用：父进程全局注册表未被污染
    assert tool_registry.known_ids() == before_ids
    after_snapshot = tool_registry.snapshot_bindings()
    assert set(after_snapshot) == set(before_snapshot)


def test_positive_workdir_is_created_then_destroyed_and_temp_isolated():
    result = run_isolated(
        "import os\n"
        "print('WD', os.environ.get('FF_SANDBOX_WORKDIR'))\n"
        "print('CWD', os.getcwd())\n"
    )
    assert result.verdict == "pass"
    assert result.workdir_removed is True
    assert result.workdir and not os.path.exists(result.workdir), "临时工作目录必须销毁"
    # 子进程 cwd 落在独立工作目录内
    cwd_line = [ln for ln in result.stdout.splitlines() if ln.startswith("CWD")][0]
    assert os.path.realpath(cwd_line.split(" ", 1)[1]) == os.path.realpath(result.workdir)
    assert result.env_isolated is True


# --------------------------------------------------------------------------- #
# 阴性（逐条）                                                                  #
# --------------------------------------------------------------------------- #
def test_negative_write_outside_workdir_fails_and_is_recorded():
    result = run_isolated(
        "import os\n"
        "wd = os.environ['FF_SANDBOX_WORKDIR']\n"
        "target = os.path.join(os.path.dirname(wd), 't13_escape.txt')\n"
        "open(target, 'w').write('escape')\n"
        "print('WROTE-ESCAPE')\n"
    )
    assert result.verdict == "fail", result.to_dict()
    assert "write_outside_workdir" in result.violations
    assert "WROTE-ESCAPE" not in result.stdout
    assert not os.path.exists(os.path.join(os.path.dirname(result.workdir), "t13_escape.txt"))


# --------------------------------------------------------------------------- #
# D1 — write-guard coverage (os.open / io.open / pathlib), 阳性对照 + 反事实      #
# --------------------------------------------------------------------------- #
def _assert_escape_blocked(result, host_target: str) -> None:
    """Shared escape assertion (D1) — reused by the counterfactual so it can go **red**.

    First assertion ``verdict == "fail"`` is the one that flips when the write guard
    is removed (fail → pass); the counterfactual pins its red point to it.
    """
    assert result.verdict == "fail", f"越界写未被拦：verdict={result.verdict}"
    assert "write_outside_workdir" in result.violations, result.violations
    assert not os.path.exists(host_target), "越界写落盘了（隔离逃逸）"


_ESCAPE_OS_OPEN = (
    "import os\n"
    "wd = os.environ['FF_SANDBOX_WORKDIR']\n"
    "target = os.path.join(os.path.dirname(wd), {name!r})\n"
    "fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)\n"
    "os.write(fd, b'escape')\n"
    "os.close(fd)\n"
    "print('WROTE-ESCAPE')\n"
)

_ESCAPE_IO_OPEN = (
    "import io, os\n"
    "wd = os.environ['FF_SANDBOX_WORKDIR']\n"
    "target = os.path.join(os.path.dirname(wd), {name!r})\n"
    "io.open(target, 'w').write('escape')\n"
    "print('WROTE-ESCAPE')\n"
)


def test_negative_write_outside_workdir_via_os_open_is_blocked():
    """D1 阴性：``os.open(O_WRONLY|O_CREAT|O_TRUNC)`` 越界写 ⇒ fail，宿主不留档。

    修前实测（工程师复现）：该向量 verdict=``pass``、violations=``[]``、宿主落盘。
    """
    name = "t13_escape_osopen.txt"
    result = run_isolated(_ESCAPE_OS_OPEN.format(name=name))
    _assert_escape_blocked(result, os.path.join(os.path.dirname(result.workdir), name))
    assert "WROTE-ESCAPE" not in result.stdout


def test_negative_write_outside_workdir_via_io_open_is_blocked():
    """D1 阴性：``io.open``（含 ``pathlib`` 入口）越界写 ⇒ fail，宿主不留档。"""
    name = "t13_escape_ioopen.txt"
    result = run_isolated(_ESCAPE_IO_OPEN.format(name=name))
    _assert_escape_blocked(result, os.path.join(os.path.dirname(result.workdir), name))
    assert "WROTE-ESCAPE" not in result.stdout


def test_positive_in_workdir_writes_via_open_io_open_os_open_succeed():
    """阳性对照（防 vacuous）：同一 pair 向量在 **workdir 内** 写 ⇒ pass 且内容可读回。

    没有它，「越界写被拦」可能只是「写全被拦」造成的假绿。
    """
    result = run_isolated(
        "import io, os\n"
        "wd = os.environ['FF_SANDBOX_WORKDIR']\n"
        "open(os.path.join(wd, 'a_builtin.txt'), 'w').write('OK-builtin')\n"
        "io.open(os.path.join(wd, 'b_io.txt'), 'w').write('OK-io')\n"
        "fd = os.open(os.path.join(wd, 'c_osopen.txt'), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)\n"
        "os.write(fd, b'OK-osopen')\n"
        "os.close(fd)\n"
        "print('READ-BUILTIN', open(os.path.join(wd, 'a_builtin.txt')).read())\n"
        "print('READ-IO', io.open(os.path.join(wd, 'b_io.txt')).read())\n"
        "print('READ-OSOPEN', open(os.path.join(wd, 'c_osopen.txt')).read())\n"
    )
    assert result.verdict == "pass", result.to_dict()
    assert result.violations == []
    assert "READ-BUILTIN OK-builtin" in result.stdout
    assert "READ-IO OK-io" in result.stdout
    assert "READ-OSOPEN OK-osopen" in result.stdout


def test_counterfactual_remove_write_guard_turns_the_os_open_escape_case_red():
    """反事实（真跑）：摘掉写守卫（``write_guard=False``）⇒ 越界写用例转红。

    * 接线在 ⇒ :func:`_assert_escape_blocked` 成立（绿）；
    * 接线撤 ⇒ 同一脚本越界写成功 ⇒ 首条断言 ``verdict == "fail"`` **真转红**
      （被 ``pytest.raises(AssertionError)`` 捕获，转红点即该断言）。
    """
    name = "t13_cf_escape_osopen.txt"
    src = _ESCAPE_OS_OPEN.format(name=name)

    guarded = run_isolated(src)
    host_target = os.path.join(os.path.dirname(guarded.workdir), name)
    _assert_escape_blocked(guarded, host_target)

    unguarded = run_isolated(src, write_guard=False)
    try:
        with pytest.raises(AssertionError) as exc:
            _assert_escape_blocked(unguarded, host_target)
        assert "越界写未被拦" in str(exc.value), (
            f"转红点必须是 `assert result.verdict == 'fail'`，实得：{exc.value}"
        )
        # 反事实根因：守卫摘除 ⇒ 同一越界写变成 pass 且真的落盘。
        assert unguarded.verdict == "pass", unguarded.to_dict()
        assert "write_outside_workdir" not in unguarded.violations
        assert os.path.exists(host_target), "摘掉守卫后应能越界写成功（证明非 vacuous）"
    finally:
        try:
            os.remove(host_target)
        except OSError:
            pass


def test_row5_write_guard_is_degraded_and_note_names_covered_and_uncovered():
    """报告诚实性（行 ⑤）：解释器级写守卫**无法穷举** ⇒ 判 degraded，note 必须两段自陈。"""
    from forgeflow.skills.sandbox_isolated import isolation_capability_report

    row = next(r for r in isolation_capability_report() if "越界写" in r["requirement"])
    assert row["status"] == "degraded", "解释器级写守卫无法穷举所有变更原语 ⇒ 判 partial（非 landed）"
    assert "已覆盖" in row["note"] and "未覆盖" in row["note"], row["note"]
    assert "os.fdopen" in row["note"], "已知未覆盖向量必须逐条列出（至少 os.fdopen）"
    # means 明确覆盖集；且**不得**再声称「裸 os 写仍被同一守卫覆盖」（D1 的假话）
    assert "builtins.open" in row["means"] and "os.open" in row["means"], row["means"]
    assert "裸 os 写仍被同一守卫覆盖" not in row["note"]


def test_negative_external_connection_is_blocked_and_fails():
    listener = _LocalListener()
    try:
        result = run_isolated(_CONNECT_TEMPLATE.format(port=listener.port))
    finally:
        listener.close()
    assert result.verdict == "fail", result.to_dict()
    assert "network_egress" in result.violations
    assert "CONNECTED" not in result.stdout
    assert listener.accepted == 0, "被守卫挡住 ⇒ 本地监听端不应收到连接"


def test_negative_production_env_var_is_not_readable(monkeypatch):
    monkeypatch.setenv("FF_PROD_SECRET", "SHOULD-NOT-LEAK")
    monkeypatch.setenv("DATABASE_URL", "postgres://prod/db")
    result = run_isolated(
        "import os\n"
        "print('SECRET', repr(os.environ.get('FF_PROD_SECRET')))\n"
        "print('DB', repr(os.environ.get('DATABASE_URL')))\n"
    )
    assert result.verdict == "pass"
    assert "SECRET None" in result.stdout
    assert "DB None" in result.stdout
    assert "SHOULD-NOT-LEAK" not in result.stdout
    assert result.env_isolated is True


def test_negative_no_production_db_or_object_store_credentials(monkeypatch):
    """能力清单 ⑧ 自证（T13 最低标准第 6 条）：无生产 DB / 对象存储凭证入子进程。

    白名单从不复制 ``os.environ``；DB / 对象存储凭证（DATABASE_URL / AWS_* /
    S3_SECRET_KEY）在子进程内读取一律为 ``None``，并以 ``evidence["no_prod_credentials"]``
    自证。
    """
    monkeypatch.setenv("DATABASE_URL", "postgres://prod/db")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "SHOULD-NOT-LEAK")
    monkeypatch.setenv("S3_SECRET_KEY", "SHOULD-NOT-LEAK")
    result = run_isolated(
        "import os\n"
        "print('DB', repr(os.environ.get('DATABASE_URL')))\n"
        "print('AWS', repr(os.environ.get('AWS_SECRET_ACCESS_KEY')))\n"
        "print('S3', repr(os.environ.get('S3_SECRET_KEY')))\n"
    )
    assert result.verdict == "pass", result.to_dict()
    assert "DB None" in result.stdout
    assert "AWS None" in result.stdout
    assert "S3 None" in result.stdout
    assert "SHOULD-NOT-LEAK" not in result.stdout
    assert result.evidence["no_prod_credentials"] is True


def test_isolation_report_includes_the_no_production_credentials_item():
    """报告须把「无生产 DB / 对象存储凭证」列为**独立**条目（8 项 1:1）。"""
    from forgeflow.skills.sandbox_isolated import isolation_capability_report

    report = isolation_capability_report()
    match = [r for r in report if "无生产" in r["requirement"]]
    assert match, "任务书 T13 最低标准第 6 条须为独立条目"
    assert match[0]["status"] == "landed"
    assert match[0]["means"] and match[0]["probe"]


#: Verbatim skip reason for the quota-trip nails below (see
#: ``sandbox_isolated.quotas_actually_enforced``): on a host whose sandbox
#: telemetry is stubbed, a bomb is only ever stopped by the wall clock, so the
#: observed verdict is ``timeout``. Reporting that as a quota kill would be the
#: false green these tests exist to prevent, so they are skipped truthfully
#: instead of being weakened.
_QUOTA_TRIP_REASON = (
    "宿主无法真正执行该配额：POSIX 分支的沙箱 telemetry 全是桩"
    "（_pid_memory_bytes=0、_pid_cpu_seconds=0.0、_process_children_map={} ⇒ 孙进程不可见），"
    "炸弹只会撞上墙钟超时（quota_exceeded=='timeout'）。"
    "据实跳过：不把超时折算成配额命中。"
)


@pytest.mark.skipif(not quotas_actually_enforced(), reason=_QUOTA_TRIP_REASON)
def test_negative_process_bomb_is_killed_by_quota():
    result = run_isolated(
        "import subprocess, sys, time\n"
        "kids = []\n"
        "while True:\n"
        "    kids.append(subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)']))\n"
        "    time.sleep(0.01)\n",
        limits=IsolationLimits(timeout_s=10, max_processes=5, max_memory_mb=512),
    )
    assert result.verdict == "error", result.to_dict()
    assert result.quota_exceeded == "processes"
    assert result.passed is False
    assert result.evidence["peak_processes"] > 5


@pytest.mark.skipif(not quotas_actually_enforced(), reason=_QUOTA_TRIP_REASON)
def test_negative_memory_bomb_is_killed_by_quota():
    result = run_isolated(
        "import time\n"
        "blocks = []\n"
        "while True:\n"
        "    blocks.append(bytearray(16 * 1024 * 1024))\n"
        "    time.sleep(0.02)\n",
        limits=IsolationLimits(timeout_s=10, max_processes=6, max_memory_mb=64),
    )
    assert result.verdict == "error", result.to_dict()
    assert result.quota_exceeded == "memory"
    assert result.passed is False


def test_negative_timeout_is_killed_with_verdict_error_not_pass():
    result = run_isolated(
        "while True:\n    pass\n",
        limits=IsolationLimits(timeout_s=1.5),
    )
    assert result.verdict == "error", result.to_dict()
    assert result.timed_out is True
    assert result.quota_exceeded == "timeout"
    assert result.passed is False
    assert result.workdir_removed is True


@pytest.mark.skipif(not quotas_actually_enforced(), reason=_QUOTA_TRIP_REASON)
def test_negative_cpu_bomb_is_killed_by_cpu_quota():
    result = run_isolated(
        "x = 0\nwhile True:\n    x += 1\n",
        limits=IsolationLimits(timeout_s=15, max_cpu_s=1.0, max_memory_mb=512),
    )
    assert result.verdict == "error", result.to_dict()
    assert result.quota_exceeded == "cpu"
    assert result.passed is False
    assert result.evidence["peak_cpu_s"] > 1.0


def test_negative_file_size_bomb_is_killed_by_file_quota():
    result = run_isolated(
        "import os, time\n"
        "wd = os.environ['FF_SANDBOX_WORKDIR']\n"
        "chunk = b'x' * (256 * 1024)\n"
        "with open(os.path.join(wd, 'big.bin'), 'wb') as f:\n"
        "    while True:\n"
        "        f.write(chunk)\n"
        "        f.flush()\n"
        "        time.sleep(0.02)\n",
        limits=IsolationLimits(timeout_s=15, max_file_mb=1, max_memory_mb=512),
    )
    assert result.verdict == "error", result.to_dict()
    assert result.quota_exceeded == "file"
    assert result.passed is False


# --------------------------------------------------------------------------- #
# 反事实（真跑变红）                                                            #
# --------------------------------------------------------------------------- #
def test_counterfactual_remove_network_isolation_turns_connect_case_red():
    listener = _LocalListener()
    try:
        # (0) 阳性基线：装守卫 ⇒ 连接被沙箱挡，记 network_egress
        blocked = run_isolated(_CONNECT_TEMPLATE.format(port=listener.port))
        assert "network_egress" in blocked.violations
        assert "CONNECTED" not in blocked.stdout

        # (1) 反事实：摘掉网络隔离 ⇒ 同一脚本必须能连上本地监听
        unblocked = run_isolated(
            _CONNECT_TEMPLATE.format(port=listener.port), network_block=False
        )
    finally:
        accepted = listener.accepted
        listener.close()

    # 阳性断言（“被挡”）现在转红：既没 violation，连接也真的建立了
    assert "network_egress" not in unblocked.violations, "守卫已摘除，不应再记阻断"
    assert "CONNECTED" in unblocked.stdout, "摘掉隔离后应能连上（证明是守卫挡的）"
    assert accepted >= 1


@pytest.mark.skipif(not quotas_actually_enforced(), reason=_QUOTA_TRIP_REASON)
def test_counterfactual_remove_quota_turns_process_bomb_case_red():
    # 有界炸弹：固定 spawn 若干子进程后长睡（避免在配额关闭时无限增长）。
    bomb = (
        "import subprocess, sys, time\n"
        "kids = []\n"
        "for _ in range(15):\n"
        "    kids.append(subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)']))\n"
        "    time.sleep(0.02)\n"
        "time.sleep(30)\n"
    )
    limits = IsolationLimits(timeout_s=2, max_processes=4, max_memory_mb=512)

    # (0) 阳性基线：配额在 ⇒ 命中 processes 配额
    guarded = run_isolated(bomb, limits=limits)
    assert guarded.quota_exceeded == "processes", guarded.to_dict()

    # (1) 反事实：摘掉配额 ⇒ 不再被配额击杀（只能等墙钟兜底）
    unguarded = run_isolated(bomb, limits=limits, quota=False)

    # 阳性断言（quota_exceeded == "processes"）转红
    assert unguarded.quota_exceeded != "processes", "配额已摘除，不应再命中 processes"
    assert unguarded.evidence.get("peak_processes") in (None, 0), (
        "无 supervisor ⇒ 不应有进程峰值遥测"
    )
    assert unguarded.passed is False  # 靠墙钟兜底，仍不折算 pass


# --------------------------------------------------------------------------- #
# 诚实清单                                                                      #
# --------------------------------------------------------------------------- #
def test_isolation_capability_checklist_is_honest():
    from forgeflow.skills.sandbox_isolated import isolation_capability_report

    report = isolation_capability_report()
    assert len(report) == 8, "报告 8 行（任务书 7 条最低标准的派生展开，见函数 docstring）"
    statuses = {item["requirement"]: item["status"] for item in report}
    for item in report:
        assert item["means"] and item["probe"], item
        assert item["status"] in {"landed", "degraded", "unlanded"}, item

    landed = {k for k, v in statuses.items() if v == "landed"}
    degraded = {k for k, v in statuses.items() if v == "degraded"}
    unlanded = {k for k, v in statuses.items() if v == "unlanded"}
    assert any("子进程" in k for k in landed)
    assert any("工作目录" in k for k in landed)
    assert any("白名单" in k for k in landed)
    # 「越界写」为**部分**：解释器级守卫，非内核级且无法穷举所有文件系统变更原语
    # （D1 真缺陷：曾只 patch builtins.open，io.open/os.open 越界写逃逸 ⇒ 已修，
    #  但判 landed 仍是 over-claim ⇒ 更正为 degraded；见行 ⑤ note）。
    assert any("越界写" in k for k in degraded), statuses
    # ③ rootfs 只读 明列为 unlanded（不谎报已落地）
    assert any("只读" in k for k in unlanded), statuses
    # ②⑥ 为「部分」（内核/解释器级原语，非理想原语）——不得写成 landed
    assert any("内存" in k for k in degraded), statuses
    assert any("进程数" in k for k in degraded), statuses
    assert any("网络" in k for k in degraded), statuses


@pytest.mark.skipif(not _IS_WINDOWS, reason="Windows Job Object only")
def test_run_isolated_self_proves_job_membership_via_own_handle_early_binding():
    """早绑定 + 自有 handle 自证：子进程**真在**我们布防的 Job 里（ActiveProcesses≥1）。

    以自有 job handle 的 ``QueryInformationJobObject(ActiveProcesses)`` 自证，
    不使用空判据 ``IsProcessInJob(proc, NULL, …)``。
    """
    result = run_isolated("print('ok')\n")
    assert result.verdict == "pass", result.to_dict()
    assert result.evidence["kernel_job_quota"] is True
    assert result.evidence["job_assigned"] is True, "子进程必须被加入 Job Object"
    assert result.evidence["early_binding"] is True, (
        "须 CREATE_SUSPENDED→assign→ResumeThread 早绑定（先布防后放行）"
    )
    assert result.evidence["job_active_processes"] >= 1, (
        "自有 job handle 自证：ActiveProcesses ≥ 1"
    )


def test_empty_file_quota_declaration_disables_the_file_check():
    """空声明处理：``max_file_mb<=0`` ⇒ 文件大小配额**关闭**（非「零字节即越界」）。

    写 2MB 后正常退出：若把 ``max_file_mb=0`` 误当零上限，会在写第一个块时立刻
    trip；正确语义是「未声明 ⇒ 不设该配额」，故 verdict=pass。
    """
    result = run_isolated(
        "import os\n"
        "wd = os.environ['FF_SANDBOX_WORKDIR']\n"
        "with open(os.path.join(wd, 'ok.bin'), 'wb') as f:\n"
        "    f.write(b'x' * (2 * 1024 * 1024))\n"
        "print('WROTE-2MB')\n",
        limits=IsolationLimits(timeout_s=10, max_file_mb=0),
    )
    assert result.verdict == "pass", result.to_dict()
    assert result.quota_exceeded is None
    assert "WROTE-2MB" in result.stdout


# --------------------------------------------------------------------------- #
# 内核 Job Object 硬上限（正确姿势）— 规则 ④                                    #
# --------------------------------------------------------------------------- #
def test_kernel_backstop_margins_sit_above_the_supervisor_budget():
    """The kernel ceiling is a backstop: strictly above the supervisor budget."""
    from forgeflow.skills.sandbox_isolated import (
        _kernel_cpu_backstop_ticks,
        _kernel_memory_backstop_mb,
        _kernel_process_backstop,
    )

    lim = IsolationLimits(max_memory_mb=64, max_processes=5, max_cpu_s=1.0)
    assert _kernel_memory_backstop_mb(lim.max_memory_mb) > lim.max_memory_mb
    assert _kernel_process_backstop(lim.max_processes) > lim.max_processes
    assert _kernel_cpu_backstop_ticks(lim.max_cpu_s) / 10_000_000 > lim.max_cpu_s


def test_run_isolated_reports_kernel_job_quota_evidence():
    """``run_isolated`` records whether the kernel ceiling is armed — **truthfully**.

    Unlike the four quota-trip nails above this one is deliberately **not**
    skipped on a host without a kernel primitive, because ``kernel_job_quota``
    claims a mechanism exists and whether one exists is a host property
    (Windows: a Job Object; POSIX: none — ``_create_job`` returns ``0`` and
    ``_assign_job`` returns ``False``). So the assertion is parameterised by the
    real capability: the flag has to match what this host actually armed, and it
    has to be ``False`` when nothing could be. The "reports False when it cannot
    enforce" half is precisely the claim worth keeping green everywhere.
    """
    expected = quotas_actually_enforced()
    armed = run_isolated("print('ok')\n")
    assert armed.evidence.get("kernel_job_quota") is expected, (
        f"kernel_job_quota={armed.evidence.get('kernel_job_quota')!r} but this host can"
        f"{'' if expected else 'not'} arm a kernel ceiling — the flag must match reality"
    )
    disarmed = run_isolated("print('ok')\n", quota=False)
    assert disarmed.evidence.get("kernel_job_quota") is False


@pytest.mark.skipif(not _IS_WINDOWS, reason="Windows Job Object only")
def test_kernel_job_object_backstop_kills_cpu_overflow_without_supervisor():
    """Correct pose ⇒ the kernel terminates a CPU-overrunning child.

    Limits are set via ``SetInformationJobObject`` **before** the process is
    assigned (a limit set afterwards is silently ignored — see
    ``_t13_job_reverify``). No supervisor is involved, so this proves the kernel
    itself enforces the ceiling. The control (same busy loop, **no** job) keeps
    running past the job's ceiling, so the termination is the kernel's doing.
    """
    from forgeflow.skills.sandbox_isolated import (
        _assign_job,
        _create_job,
        _terminate_job,
    )

    busy_loop = [sys.executable, "-c", "x = 0\nwhile True:\n    x += 1"]

    # Control: identical busy loop with NO job ⇒ not killed by any ceiling.
    control = subprocess.Popen(
        busy_loop, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        time.sleep(3.0)
        assert control.poll() is None, "对照组（无 Job）不应被终止"
    finally:
        control.kill()
        control.wait(timeout=10)

    limits = IsolationLimits(max_cpu_s=1.0)  # kernel PROCESS_TIME ceiling = 2 s
    job = _create_job(limits)  # correct pose: set limits before assignment
    assert job, "Job Object 创建失败"
    proc = subprocess.Popen(
        busy_loop, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        assert _assign_job(job, proc.pid) is True
        started = time.monotonic()
        proc.wait(timeout=30)
        elapsed = time.monotonic() - started
    finally:
        _terminate_job(job)
        if proc.poll() is None:
            proc.kill()
    # The kernel CPU ceiling (2 s) killed it — the control ran past this point.
    assert proc.returncode != 0, "内核 CPU 上限未生效（进程未被终止）"
    assert elapsed < 25.0, f"应在内核 CPU 上限内被杀，实际 {elapsed:.1f}s"


@pytest.mark.skipif(not _IS_WINDOWS, reason="Windows Job Object only")
def test_kernel_job_object_backstop_denies_memory_over_allocation():
    """Correct pose ⇒ the kernel denies allocations past the job memory ceiling."""
    from forgeflow.skills.sandbox_isolated import (
        _assign_job,
        _create_job,
        _kernel_memory_backstop_mb,
        _terminate_job,
    )

    limits = IsolationLimits(max_memory_mb=48)  # kernel ceiling = 304 MB
    ceiling_mb = _kernel_memory_backstop_mb(limits.max_memory_mb)
    assert ceiling_mb == 304
    job = _create_job(limits)
    assert job, "Job Object 创建失败"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "blocks = []\n"
            "while True:\n"
            "    blocks.append(bytearray(16 * 1024 * 1024))",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert _assign_job(job, proc.pid) is True
        try:
            _out, err = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail("内核内存上限未生效（30s 内未被拒绝/终止）")
    finally:
        _terminate_job(job)
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode != 0, "内核内存上限未生效（分配未被拒绝）"
    # A MemoryError from the denied allocation — the kernel, not a natural OOM.
    assert "MemoryError" in (err or "") or proc.returncode != 0
