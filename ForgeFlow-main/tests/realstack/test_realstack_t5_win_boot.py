"""T5 — Windows 启动链回归：真实的 Uvicorn 子进程必须跑在 SelectorEventLoop 上。

为什么必须起**子进程**而不是 TestClient
-----------------------------------------
本用例要证的是 "Uvicorn → FastAPI → psycopg/asyncpg → PostgreSQL" 这条**真实
启动链**跑在 Selector 事件循环上。而事件循环是 uvicorn 的 ``Server.run`` 在
``_compat.asyncio_run(..., loop_factory=config.get_loop_factory())`` 里决定的
（uvicorn 0.53.0：``win32 and not use_subprocess`` ⇒ ``ProactorEventLoop``）。
TestClient 根本不经过 uvicorn 的 loop factory，用它测这条链路等于没测。

被验证的 7 步（全部来自**子进程自己的日志/HTTP 响应**，无一处靠推断）：
  1. Uvicorn 真实子进程启动
  2. SelectorEventLoop 实际生效（``[BOOT] event_loop=`` 由**运行中的循环内**打印）
  3. FastAPI startup 成功
  4. PostgreSQL pool ready
  5. ``/health`` == 200 且 ``database == "connected"``
  6. DB-backed endpoint 正常（``GET /workspaces/`` 真查 PG；无 token 必须 401）
  7. shutdown 正确关闭 pool（``Application shutdown complete`` + ``[DB] ... closed``）

并分别验证 ``reload=False`` 与 ``reload=True``。

门控
----
沿用 ``tests/realstack/conftest.py`` 的 ``realstack`` fixture：
``FORGEFLOW_REAL_STACK=1`` + Ollama 探活 + PostgreSQL 探活，任一不满足即
**skip 并带明确 reason** —— 绝不把 SKIP 写成 PASS。

未使用 ``pytest.mark.asyncio``：本用例是同步的（子进程 + urllib），
``asyncio_mode=auto`` 下按普通同步用例收集。
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

# ``subprocess.CREATE_NEW_PROCESS_GROUP`` 与 ``signal.CTRL_BREAK_EVENT`` 只在 win32
# 存在。这里**故意**用 ``getattr`` 取值而不是直接写属性名：mypy 按**宿主平台**的
# typeshed 判定属性是否存在，所以 Linux CI 上裸写 ``subprocess.CREATE_NEW_PROCESS_GROUP``
# 会报 ``attr-defined``（本模块只在 Windows 启动链上跑，其余平台由 realstack fixture
# 门控 skip）。请勿"顺手"改回裸属性访问 —— 那会再次把 Linux CI 的 typecheck 打红。
# 非 win32 取到 0：这两个常量仅在 win32 路径被真正使用。
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
_CTRL_BREAK_EVENT = getattr(signal, "CTRL_BREAK_EVENT", 0)

#: ForgeFlow-main 仓库根（本文件在 <root>/tests/realstack/ 下）。
PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
BOOT_MODULE = "forgeflow.bootstrap"

#: dev 登录账号（.env: DEV_LOGIN_ENABLED=true / DEV_LOGIN_PASSWORD=forgeflow-dev）。
#: 密码从 Settings 读，不硬编码 —— 改了 .env 这里不会漂。
ADMIN_USER = "admin"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def _child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """子进程环境：真实栈配置 + 绕开沙箱代理。

    ``tests/conftest.py`` 用 setdefault 把离线档钉成 memory/mock；这里**显式**
    覆盖回 postgres/ollama，否则"真实栈"会变成假测试。
    """
    env = dict(os.environ)
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        env.pop(key, None)
    env["NO_PROXY"] = "127.0.0.1,localhost,::1"
    env["PYTHONUTF8"] = "1"
    env["STORAGE_BACKEND"] = "postgres"
    env["LLM_PROVIDER"] = "ollama"
    if extra:
        env.update(extra)
    return env


def _http(
    port: int,
    path: str,
    *,
    token: str | None = None,
    method: str = "GET",
    payload: dict | None = None,
    timeout: float = 20.0,
) -> tuple[int, object]:
    """Return (status, parsed_json_or_text).

    Never raises: a not-yet-listening port surfaces as status ``0`` with the
    exception text as the body, so the readiness poll can retry instead of
    blowing up on the first connection-refused.
    """
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, body
    except urllib.error.URLError as exc:
        # port not listening yet (boot in progress) / connection reset
        return 0, f"{type(exc).__name__}: {exc}"
    except OSError as exc:
        return 0, f"{type(exc).__name__}: {exc}"


class BootedServer:
    """A REAL uvicorn child process, started through ``forgeflow.bootstrap``."""

    def __init__(self, reload: bool, log_path: pathlib.Path, extra_env=None) -> None:
        self.reload = reload
        self.log_path = log_path
        self.extra_env = extra_env or {}
        self.port = _free_port()
        self.proc: subprocess.Popen | None = None
        self._fh: io.TextIOWrapper | None = None

    # -- lifecycle -------------------------------------------------------- #
    def start(self, timeout: float = 180.0) -> BootedServer:
        cmd = [
            sys.executable,
            "-m",
            BOOT_MODULE,
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--log-level",
            "info",
        ]
        if self.reload:
            cmd.append("--reload")

        self._fh = self.log_path.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            env=_child_env(self.extra_env),
            stdout=self._fh,
            stderr=subprocess.STDOUT,
            # Own process group ⇒ CTRL_BREAK_EVENT can be delivered to it, which
            # is uvicorn's Windows graceful-shutdown signal (Server.HANDLED_SIGNALS
            # includes SIGBREAK on win32).
            creationflags=_CREATE_NEW_PROCESS_GROUP,
        )

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError(
                    f"uvicorn 子进程在就绪前退出 (reload={self.reload}, "
                    f"returncode={self.proc.returncode})。日志尾部：\n{self.log_tail()}"
                )
            status, _ = _http(self.port, "/health", timeout=3.0)
            if status == 200:
                return self
            time.sleep(1.5)
        raise AssertionError(
            f"uvicorn 子进程 {timeout}s 内未就绪 (reload={self.reload})。"
            f"日志尾部：\n{self.log_tail()}"
        )

    def stop(self, timeout: float = 40.0) -> tuple[bool, int | None]:
        """Send CTRL_BREAK (graceful) and wait. Returns (exited, returncode).

        返回码在 Windows 上**非 0** 是预期行为：uvicorn 的 ``capture_signals``
        在 finally 里把捕获到的信号 ``raise_signal`` 重放，MSVCRT 对 SIGBREAK 的
        默认动作就是 ``exit(3)``（reload 档的 supervisor 则是
        ``0xC000013A`` STATUS_CONTROL_C_EXIT）。所以"是否优雅关闭"**只能**看
        日志里的 shutdown 序列，不能用 returncode 断言。
        """
        assert self.proc is not None
        exited = False
        if self.proc.poll() is None:
            try:
                os.kill(self.proc.pid, _CTRL_BREAK_EVENT)
            except OSError:
                pass
            deadline = time.time() + timeout
            while time.time() < deadline:
                if self.proc.poll() is not None:
                    exited = True
                    break
                time.sleep(0.5)
        else:
            exited = True

        if self.proc.poll() is None:  # last resort — kill the whole tree
            self._force_kill()
        if self._fh is not None:
            self._fh.flush()
            self._fh.close()
            self._fh = None
        return exited, self.proc.returncode

    def _force_kill(self) -> None:
        assert self.proc is not None
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                capture_output=True,
                timeout=25,
            )
        except Exception:  # noqa: BLE001 — best effort
            self.proc.kill()
        try:
            self.proc.wait(timeout=15)
        except Exception:  # noqa: BLE001
            pass

    # -- observation ------------------------------------------------------ #
    def log_text(self) -> str:
        if not self.log_path.is_file():
            return ""
        return self.log_path.read_text(encoding="utf-8", errors="replace")

    def log_tail(self, n: int = 2500) -> str:
        return self.log_text()[-n:]

    def boot_value(self, key: str) -> str | None:
        """Value of the last ``[BOOT] <key>=<value>`` line, or None."""
        found = None
        for line in self.log_text().splitlines():
            marker = f"[BOOT] {key}="
            if marker in line:
                found = line.split(marker, 1)[1].strip()
        return found


# --------------------------------------------------------------------------- #
# the test — parameterised over reload=False / reload=True
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("reload", [False, True], ids=["reload=False", "reload=True"])
def test_t5_win_boot_chain_on_selector_loop(realstack, tmp_path, reload):
    """真实 Uvicorn 子进程 → SelectorEventLoop → FastAPI → PostgreSQL 全链。"""
    from forgeflow.config import get_settings

    # 注意：**本 pytest 进程**被 ``tests/conftest.py`` 刻意钉在离线档
    # （STORAGE_BACKEND=memory / LLM_PROVIDER=mock），这是为了离线快测的封闭性。
    # 因此这里**不能**断言进程内 settings 是 postgres —— 子进程的档位由
    # ``_child_env()`` 显式覆盖，并在下面用它自己的 ``[BOOT] storage_backend=``
    # 日志行来证明（那才是被验对象的事实来源）。
    settings = get_settings()
    password = settings.dev_login_password.get_secret_value()
    assert password, "DEV_LOGIN_PASSWORD 为空，无法走 /auth/login 验证 DB 端点"

    log_path = tmp_path / f"boot_reload_{reload}.log"
    server = BootedServer(reload=reload, log_path=log_path)
    try:
        # --- 1. real uvicorn subprocess starts ------------------------- #
        server.start()
        assert server.proc is not None
        assert server.proc.poll() is None, "子进程应在启动后保持存活"
        assert "Started server process" in server.log_text(), (
            f"日志里没有 uvicorn 的 'Started server process' —— 子进程没真的起 uvicorn。"
            f"\n{server.log_tail()}"
        )

        # --- 2. SelectorEventLoop actually in effect ------------------- #
        loop_name = server.boot_value("event_loop")
        assert loop_name, (
            "日志里没有 `[BOOT] event_loop=` 行 —— 启动诊断没生效，"
            f"无法证明真实事件循环。\n{server.log_tail()}"
        )
        assert "Selector" in loop_name, (
            f"真实事件循环是 {loop_name!r}，不是 Selector —— Windows + psycopg 的"
            "根因没有修好（reload=False 在 uvicorn 0.53 上默认拿 Proactor）。"
        )
        # 反证：修复前的失败形态是 ProactorEventLoop。它一旦出现，本用例必须红。
        assert "Proactor" not in server.log_text(), (
            "日志里出现了 ProactorEventLoop —— 这正是被修的故障形态，不允许同时出现。"
        )
        # 诊断块必须证明跑的是 postgres 档，否则"没碰 psycopg"也能蒙混过关。
        assert server.boot_value("storage_backend") == "postgres", (
            f"子进程实际 storage_backend={server.boot_value('storage_backend')!r}，与用例前提不符。"
        )

        # --- 3. FastAPI startup succeeded ------------------------------ #
        assert "Application startup complete" in server.log_text(), (
            f"FastAPI 没有完成启动。\n{server.log_tail()}"
        )

        # --- 4. PostgreSQL pool ready ---------------------------------- #
        assert "[DB] PostgreSQL pool ready" in server.log_text(), (
            f"psycopg/asyncpg 侧没有报 pool ready。\n{server.log_tail()}"
        )
        assert "[DB] PostgreSQL pool initialization FAILED" not in server.log_text(), (
            f"pool 初始化报了失败。\n{server.log_tail()}"
        )

        # --- 5. /health == 200 AND a real pool ------------------------- #
        status, body = _http(server.port, "/health")
        assert status == 200, f"/health 返回 {status}，body={body!r}"
        assert isinstance(body, dict), f"/health 返回体不是 JSON 对象：{body!r}"
        assert body.get("database") == "connected", (
            f"/health 报告 database={body.get('database')!r} —— pool 不在。"
        )

        # --- 6. DB-backed endpoint works (RBAC negative + positive) ---- #
        # 负向对照：没有 token 必须 401 —— 证明打到的确实是带 RBAC 中间件的真 app，
        # 而不是某种被绕过/替身的桩。
        anon_status, _ = _http(server.port, "/workspaces/")
        assert anon_status == 401, (
            f"无 token 访问 /workspaces/ 得到 {anon_status}，期望 401 —— "
            "RBAC 中间件没生效，这个阳性结果不可信。"
        )

        login_status, login_body = _http(
            server.port,
            "/auth/login",
            method="POST",
            payload={"user_id": ADMIN_USER, "password": password},
        )
        assert login_status == 200, (
            f"/auth/login 返回 {login_status}：{login_body!r} —— "
            "凭证存储（PostgreSQL）未就绪，DB 端点无法验证。"
        )
        assert isinstance(login_body, dict) and login_body.get("access_token"), (
            f"/auth/login 没给出 access_token：{login_body!r}"
        )
        token = login_body["access_token"]

        ws_status, ws_body = _http(server.port, "/workspaces/", token=token)
        assert ws_status == 200, (
            f"带 admin token 访问 /workspaces/ 返回 {ws_status}：{ws_body!r} —— "
            "DB-backed 端点没有正常工作。"
        )
        assert isinstance(ws_body, list), (
            f"/workspaces/ 应返回 JSON 列表（真查 PG 的结果），实际 {type(ws_body).__name__}"
        )

        # --- 7. shutdown closes the pool ------------------------------- #
        exited, returncode = server.stop()
        assert exited, f"收到 CTRL_BREAK 后子进程仍未退出。\n{server.log_tail()}"
        log_after = server.log_text()
        assert "Application shutdown complete" in log_after, (
            f"lifespan 没有走完 shutdown（pool 可能仍持有连接）。"
            f"\nreturncode={returncode}\n{server.log_tail()}"
        )
        assert "[DB] PostgreSQL pool closed" in log_after, (
            f"pool 没有被关闭（缺 `[DB] PostgreSQL pool closed`）。"
            f"\nreturncode={returncode}\n{server.log_tail()}"
        )
    finally:
        if server.proc is not None and server.proc.poll() is None:
            server.stop()
