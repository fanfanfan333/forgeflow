"""INC46 T13 — container isolation backend: kernel-isolated execution (裁定 I).

Scope (阳性 / 阴性 / 诚实清单 / 显式降级):

* **阳性** — 代码**真的**在容器里跑（真实计算 + 退出码回传），--rm 无残骸。
* **阴性（逐条）** — 写根（只读）/ 外联（无网）/ 内存炸弹（OOM）/ fork 炸弹（pids）
  ⇒ 全部**非 pass**；env 白名单（宿主 secret 读不到）。
* **显式降级** — Docker 不可用时 ``run_isolated_docker`` 返回 ``error`` +
  ``not_supported=True``，**不静默降级**、**不记 PASS**（红线 10）；需要 Docker 的
  用例显式 ``skip`` 并附原因。
* **诚实清单 / 差异表** — 容器后端 8 项逐条 + native↔container 能力差异表。

需要 Docker 守护进程可用（``docker version``）；不可用则相关用例 **skip**（附原因），
**不得记 PASS**。
"""

from __future__ import annotations

import pytest

from forgeflow.sandbox import docker_isolated as di

_DOCKER_UP = di.docker_isolation_available()
_skip_no_docker = pytest.mark.skipif(
    not _DOCKER_UP,
    reason=f"{di.NOT_SUPPORTED}: Docker 守护进程不可用 —— 容器后端用例 skip（不记 PASS）",
)


# --------------------------------------------------------------------------- #
# 显式降级（无需 Docker）                                                        #
# --------------------------------------------------------------------------- #
def test_run_isolated_docker_reports_not_supported_when_daemon_absent(monkeypatch):
    """Docker 不可用 ⇒ 显式 NOT_SUPPORTED（error，绝不是 pass / 静默降级）。"""
    monkeypatch.setattr(di, "docker_version", lambda *a, **k: None)

    result = di.run_isolated_docker("print('should not run')")

    assert result.verdict == "error"
    assert result.passed is False
    assert result.evidence["docker_available"] is False
    assert result.evidence["not_supported"] is True
    assert di.NOT_SUPPORTED in result.evidence["reason"]
    assert result.stdout == "", "Docker 不可用时不得执行任何用户代码"


def test_backend_capability_diff_table_is_present_and_honest():
    """裁定 I：须交付后端能力差异表（native vs container），且 native 不得 over-claim。"""
    rows = di.backend_capability_diff_table()
    by_key = {r["能力"]: r for r in rows}
    assert any("只读" in k for k in by_key), by_key
    assert any("网络" in k for k in by_key), by_key
    # ⑤ 只读根：原生 unlanded，容器 landed（容器才是落地路线）
    ro = next(r for k, r in by_key.items() if "只读" in k)
    assert ro["native"] == "unlanded" and ro["container"] == "landed"
    # ⑧ 网络：原生 degraded，容器 landed
    net = next(r for k, r in by_key.items() if "网络" in k)
    assert net["native"] == "degraded" and net["container"] == "landed"


def test_backend_capability_diff_table_aligns_to_the_report_eight_rows():
    """D2：diff table 的前 8 行**顺序 + 编号**对齐 ``isolation_capability_report()``；
    多出的能力项作为 ``[附加]`` 行排在 8 行之后。"""
    from forgeflow.skills.sandbox_isolated import isolation_capability_report

    rows = di.backend_capability_diff_table()
    report = isolation_capability_report()
    numerals = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧"]

    head = rows[:8]
    assert len(head) == 8, rows
    for i, num in enumerate(numerals):
        assert head[i]["能力"].startswith(num), head[i]
        assert head[i]["能力"][:2] == report[i]["requirement"][:2], (head[i], report[i])

    extras = rows[8:]
    assert extras, "报告 8 行之外的能力项应以附加行列出"
    assert all("[附加]" in r["能力"] for r in extras), extras
    assert len([r for r in rows if "[附加]" not in r["能力"]]) == 8, "非附加行必须恰为 8 行"
    # ⑤ 越界写：原生解释器级守卫无法穷举 ⇒ degraded（与报告一致，不得 over-claim）
    write_row = next(r for r in rows if "越界写" in r["能力"])
    assert write_row["native"] == "degraded" and write_row["container"] == "landed"


def test_docker_capability_report_covers_the_eight_items_and_lands_the_missing_four():
    report = di.docker_isolation_capability_report()
    assert len(report) == 8, "任务书八项逐条"
    statuses = {item["requirement"]: item["status"] for item in report}
    for item in report:
        assert item["means"] and item["probe"], item
        assert item["status"] in {"landed", "degraded", "unlanded", di.NOT_SUPPORTED}
    # 容器路线把原生 ③（只读根, unlanded）/②⑥（网络 / 配额, degraded）全部转 landed
    for needle in ("只读", "内存", "进程数", "网络"):
        key = next(k for k in statuses if needle in k)
        assert statuses[key] == "landed", (needle, statuses[key])


def test_docker_report_includes_the_no_production_credentials_item():
    """容器报告同样须含「无生产 DB / 对象存储凭证」独立条目（8 项 1:1）。"""
    report = di.docker_isolation_capability_report()
    match = [r for r in report if "无生产" in r["requirement"]]
    assert match, "任务书 T13 最低标准第 6 条须为独立条目"
    assert match[0]["status"] == "landed"
    assert match[0]["means"] and match[0]["probe"]


# --------------------------------------------------------------------------- #
# 阳性（需 Docker）                                                              #
# --------------------------------------------------------------------------- #
@_skip_no_docker
def test_positive_real_container_execution_with_zero_residue():
    result = di.run_isolated_docker(
        "total = sum(i * i for i in range(1000))\nprint('RESULT', total)\n"
    )
    assert result.verdict == "pass", result.to_dict()
    assert result.passed is True
    assert result.stdout.strip() == f"RESULT {sum(i * i for i in range(1000))}"
    assert result.returncode == 0
    assert result.evidence["docker_available"] is True
    assert result.workdir_removed is True


@_skip_no_docker
def test_negative_read_only_rootfs_blocks_write_outside_tmpfs():
    result = di.run_isolated_docker("open('/x', 'w').write('a')\nprint('WROTE-ROOT')\n")
    assert result.verdict == "fail", result.to_dict()
    assert "read_only_rootfs" in result.violations
    assert "WROTE-ROOT" not in result.stdout


@_skip_no_docker
def test_negative_network_egress_is_blocked_kernel_level():
    result = di.run_isolated_docker(
        "import socket\n"
        "socket.create_connection(('1.1.1.1', 80), timeout=3)\n"
        "print('CONNECTED')\n"
    )
    assert result.verdict == "fail", result.to_dict()
    assert "network_egress" in result.violations
    assert "CONNECTED" not in result.stdout


@_skip_no_docker
def test_negative_memory_bomb_is_oom_killed():
    result = di.run_isolated_docker(
        "a = []\nwhile True:\n    a.append('x' * (10 ** 6))\n",
        limits=di.DockerLimits(memory_mb=64, timeout_s=60),
    )
    assert result.verdict == "fail", result.to_dict()
    assert result.returncode == 137, "64MB 配额下应为 OOM 击杀（exit 137）"
    assert result.quota_exceeded == "memory"


@_skip_no_docker
def test_negative_process_bomb_is_limited_by_pids():
    result = di.run_isolated_docker(
        "import subprocess\n"
        "[subprocess.Popen(['sleep', '30']) for _ in range(200)]\n",
        limits=di.DockerLimits(pids_limit=32, timeout_s=60),
    )
    assert result.verdict == "fail", result.to_dict()
    assert result.returncode != 0


@_skip_no_docker
def test_negative_production_env_var_is_not_readable(monkeypatch):
    monkeypatch.setenv("PROD_SECRET", "SHOULD-NOT-LEAK")
    result = di.run_isolated_docker(
        "import os\nprint('SECRET', os.environ.get('PROD_SECRET'))\n"
    )
    assert result.verdict == "pass", result.to_dict()
    assert result.stdout.strip() == "SECRET None"
    assert "SHOULD-NOT-LEAK" not in result.stdout


@_skip_no_docker
def test_negative_no_production_db_or_object_store_credentials(monkeypatch):
    """能力清单 ⑧ 自证：容器只拿到显式 -e 变量，宿主 DB / 对象存储凭证不入容器。"""
    monkeypatch.setenv("DATABASE_URL", "postgres://prod/db")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "SHOULD-NOT-LEAK")
    result = di.run_isolated_docker(
        "import os\n"
        "print('DB', repr(os.environ.get('DATABASE_URL')))\n"
        "print('AWS', repr(os.environ.get('AWS_SECRET_ACCESS_KEY')))\n"
    )
    assert result.verdict == "pass", result.to_dict()
    assert "DB None" in result.stdout
    assert "AWS None" in result.stdout
    assert "SHOULD-NOT-LEAK" not in result.stdout
    assert result.evidence["no_prod_credentials"] is True


@_skip_no_docker
def test_negative_timeout_is_killed_with_verdict_error_not_pass():
    result = di.run_isolated_docker(
        "import time\ntime.sleep(30)\n",
        limits=di.DockerLimits(timeout_s=3),
    )
    assert result.verdict == "error", result.to_dict()
    assert result.timed_out is True
    assert result.passed is False
