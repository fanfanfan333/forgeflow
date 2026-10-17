"""WS-B independent verification — #6「全离线档」真启动回归（真 uvicorn，非 tests/**）。

归属：software-engineer-2 独立复验（三层里的第二层）。
被测：#6 / WS-E1 —— api/main.py 让 memory 档在 PG 不可达时也能启动。

用法：
    python qa_tmp/wsb_verify_offline_boot.py memory     # 期望：真起服务、/health 200、pool=None
    python qa_tmp/wsb_verify_offline_boot.py postgres   # 期望（阴性对照）：启动失败

固定：端口 8012；POSTGRES_URL 指向死端口 127.0.0.1:1；LLM/EMBEDDING=mock。
脚本只读运行中的应用对象，不改任何源码/测试。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request

# Make the project root importable regardless of where this script is invoked from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROFILE = (sys.argv[1] if len(sys.argv) > 1 else "memory").strip().lower()
HOST = "127.0.0.1"
PORT = 8012
BASE = f"http://{HOST}:{PORT}"

# --- 环境必须在 import forgeflow 之前落定（settings 在 import 期即构建）---
os.environ["STORAGE_BACKEND"] = PROFILE
os.environ["POSTGRES_URL"] = "postgresql+asyncpg://forgeflow:forgeflow@127.0.0.1:1/forgeflow"
os.environ["POSTGRES_SYNC_URL"] = "postgresql+psycopg://forgeflow:forgeflow@127.0.0.1:1/forgeflow"
os.environ["LLM_PROVIDER"] = "mock"
os.environ["EMBEDDING_PROVIDER"] = "mock"
os.environ["API_HOST"] = HOST
os.environ["API_PORT"] = str(PORT)

import uvicorn  # noqa: E402

from forgeflow.api.main import app  # noqa: E402

_CAPTURED: list[str] = []
_EXC: list[BaseException] = []


class _Capture(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info:
                msg += "\n" + logging.Formatter().formatException(record.exc_info)
            _CAPTURED.append(msg)
        except Exception:  # noqa: BLE001
            pass


def _http_get(path: str, timeout: float = 5.0) -> tuple[int, str]:
    """Return (status, body) or (0, error) when unreachable."""
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:  # non-2xx is still a live server
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def _http_post(path: str, payload: dict, timeout: float = 5.0) -> tuple[int, str]:
    """POST JSON; return (status, body) or (0, error)."""
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def _boot() -> tuple[uvicorn.Server, threading.Thread]:
    logging.getLogger().addHandler(_Capture())
    # log_config=None: keep OUR root logging (set by main.py) so uvicorn's
    # "Application startup failed." message reaches the capture handler instead
    # of uvicorn installing its own non-propagating logger config.
    config = uvicorn.Config(
        app, host=HOST, port=PORT, log_level="info", lifespan="on", log_config=None
    )
    server = uvicorn.Server(config)

    def _run() -> None:
        try:
            server.run()  # asyncio.run(serve()) inside this thread
        except BaseException as exc:  # noqa: BLE001
            _EXC.append(exc)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return server, thread


def _wait_started(server: uvicorn.Server, thread: threading.Thread, timeout: float = 25.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if server.started or not thread.is_alive():
            return
        time.sleep(0.2)


def _line(subject: str, ok: bool, observed: object) -> None:
    print(f"  [{'OK ' if ok else 'BAD'}] {subject}: {observed}")


def main() -> int:
    print(f"=== #6 复验 profile={PROFILE} | {BASE} | PG DSN -> dead 127.0.0.1:1 ===")
    server, thread = _boot()
    _wait_started(server, thread)

    if PROFILE != "memory":
        # 阴性对照：postgres + 不可达 PG → 启动必须失败（证明不是"两档都不连库"的过度修复）
        print("\n[阴性对照 · postgres 档 + 死 PG]")
        body = "\n".join(_CAPTURED)
        failed = (not server.started) and (not thread.is_alive())
        has_startup_failure = "startup failed" in body.lower()
        has_refused = any(
            "ConnectionRefusedError" in m or "connect" in m.lower() for m in _CAPTURED
        )
        _line("server 未 started", not server.started, server.started)
        _line("server 线程已退出（启动失败信号）", not thread.is_alive(), thread.is_alive())
        _line("启动失败（期望 True）", failed, failed)
        _line("uvicorn 日志含 'startup failed'（期望 True）", has_startup_failure, has_startup_failure)
        _line("日志含连接失败证据（期望 True）", has_refused, has_refused)
        status, _b = _http_get("/health")
        print(f"  [obs] /health status = {status}  （非断言：端口可能被代理/残留 socket 影响）")
        verdict = failed and has_startup_failure
        print(f"\nNEGATIVE-CONTROL VERDICT: {'PASS' if verdict else 'FAIL'}")
        print("--- captured log (tail) ---")
        print("\n".join(_CAPTURED[-20:]))
        return 0 if verdict else 2

    # ---- memory 档：真起服务，逐条断言 ----
    print("\n[startup] server.started =", server.started, "| thread alive =", thread.is_alive())
    if not server.started:
        print("!!! memory 档未能启动 —— 复现了 #6 的缺陷")
        print("--- captured log ---")
        print("\n".join(_CAPTURED))
        return 3

    status, health_body = _http_get("/health")
    print(f"\n[GET /health] status={status} body={health_body}")
    try:
        health = json.loads(health_body)
    except Exception:  # noqa: BLE001
        health = {}

    pool = getattr(app.state, "pool", "MISSING")
    escalation_job = getattr(app.state, "escalation_job", "MISSING")
    graph = getattr(app.state, "graph", None)

    from forgeflow.graph.checkpointer import _checkpointer  # module-global singleton

    cp_name = type(_checkpointer).__name__ if _checkpointer is not None else "None"
    graph_cp = getattr(graph, "checkpointer", None)
    graph_cp_name = type(graph_cp).__name__ if graph_cp is not None else "None"

    skills_status, skills_body = _http_get("/skills")
    login_status, login_body = _http_post(
        "/auth/login", {"username": "admin", "password": "forgeflow-dev"}
    )

    print("\n[断言 · startup 链路]")
    _line("app.state.pool is None", pool is None, repr(pool))
    _line("/health status == 200", status == 200, status)
    _line("/health database == 'unavailable'", health.get("database") == "unavailable", health.get("database"))
    _line("/health graph == 'compiled'", health.get("graph") == "compiled", health.get("graph"))
    _line("checkpointer is InMemorySaver (module)", cp_name == "InMemorySaver", cp_name)
    _line("compiled graph.checkpointer is InMemorySaver", graph_cp_name == "InMemorySaver", graph_cp_name)
    _line("app.state.escalation_job is None", escalation_job is None, repr(escalation_job))

    guards = {
        "pool-skip log present": any("PostgreSQL pool skipped" in m for m in _CAPTURED),
        "escalation-skip log present": any("escalation job skipped" in m for m in _CAPTURED),
        "no demo-user seeding (pool None)": not any("demo users" in m for m in _CAPTURED),
        "no traceback in startup log": not any("Traceback" in m for m in _CAPTURED),
    }
    print("\n[断言 · 四处守卫「跳过而非异常」]")
    for name, ok in guards.items():
        _line(name, ok, ok)

    print("\n[观察值 · 不作断言]")
    print(f"  GET  /skills        -> status={skills_status} body={skills_body[:120]}")
    print(f"  POST /auth/login    -> status={login_status} body={login_body[:120]} (admin/forgeflow-dev)")
    print("    （观察值：/skills=401 表示路由可达且 RBAC 生效；login 语义归 Task #9，#6 不作断言）")

    # graceful shutdown — 第 4 处守卫（shutdown 分支不得因 pool=None 报错）
    print("\n[shutdown] 触发优雅停机")
    server.should_exit = True
    thread.join(timeout=15)
    shutdown_ok = not thread.is_alive()
    _line("server 线程已退出（无 shutdown 异常）", shutdown_ok, shutdown_ok)
    _line("shutdown 期无异常", not _EXC, [type(e).__name__ for e in _EXC])

    all_ok = (
        pool is None
        and status == 200
        and health.get("database") == "unavailable"
        and cp_name == "InMemorySaver"
        and graph_cp_name == "InMemorySaver"
        and escalation_job is None
        and all(guards.values())
        and shutdown_ok
        and not _EXC
    )
    print(f"\nMEMORY-PROFILE VERDICT: {'PASS' if all_ok else 'FAIL'}")
    print("--- captured startup log (tail) ---")
    print("\n".join(_CAPTURED[-30:]))
    return 0 if all_ok else 4


if __name__ == "__main__":
    raise SystemExit(main())
