"""Shared gating + fixtures for the real-stack suite (Ollama + PostgreSQL).

门控（gate）
-----------
每个用例都通过 ``realstack`` fixture 走同一道门：

  * ``FORGEFLOW_REAL_STACK`` != ``"1"``  ⇒ skip（离线快测默认路径）；
  * Ollama 的 ``GET {OLLAMA_BASE_URL}/api/tags`` 打不通 ⇒ skip；
  * PostgreSQL 连不上 / 缺表 ⇒ skip。

skip 一律带**明确 reason**（服务名 + URL + 真实异常），绝不允许"静默跳过"
把真实栈用例伪装成绿的。

真实栈环境（realstack_env）
--------------------------
``tests/conftest.py`` 为了离线封闭性把 ``STORAGE_BACKEND`` 设成 ``memory``、
``LLM_PROVIDER`` 设成 ``mock``。真实栈用例必须显式把它们改回
``postgres`` / ``ollama`` 并清掉 ``get_settings`` 的 lru 缓存，否则读到的仍
是离线配置——那会让"真实栈"变成名不副实的假测试。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import urllib.request
from typing import Any

import pytest

# --------------------------------------------------------------------------- #
# 0. 代理：本机调用必须绕过沙箱 HTTP 代理，否则 127.0.0.1:11434 会被代理成 502，
#    引擎会静默降级到 mock（这正是真实栈测试要抓的假象）。
# --------------------------------------------------------------------------- #
for _k in (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "127.0.0.1,localhost,::1"

GATE_ENV_VAR = "FORGEFLOW_REAL_STACK"

#: 真实跑一次 run 的 run_id —— 会话结束统一清理，避免污染开发库。
_CLEANUP_RUN_IDS: set[str] = set()
#: 真实跑一次很贵（一次 ~4k token / 8s），同一进程内按 tag 复用。
_REAL_RUNS: dict[str, dict[str, Any]] = {}


def _gate_enabled() -> bool:
    return os.environ.get(GATE_ENV_VAR, "").strip() == "1"


# --------------------------------------------------------------------------- #
# 1. 探测                                                                      #
# --------------------------------------------------------------------------- #
def _probe_ollama(base_url: str, timeout: float = 5.0) -> tuple[bool, str]:
    """``GET /api/tags`` —— 真探活，返回 (ok, 说明)。"""
    url = base_url.rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            names = [m.get("name") for m in body.get("models", [])]
            return True, f"models={names}"
    except Exception as exc:  # noqa: BLE001 — 探测就是要吞掉所有失败
        return False, f"{type(exc).__name__}: {exc}"


def _probe_pg(dsn: str, timeout: float = 6.0) -> tuple[bool, str]:
    """真连一次 PostgreSQL（独立 asyncpg 连接），返回 (ok, 说明)。"""

    async def _go() -> tuple[bool, str]:
        import asyncpg

        conn = await asyncpg.connect(dsn, timeout=timeout)
        try:
            ver = await conn.fetchval("SELECT version()")
            tables = await conn.fetch(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
            )
            names = {t["table_name"] for t in tables}
            missing = sorted({"workspace_runs", "experiences"} - names)
            if missing:
                return False, f"缺少表 {missing}（migration 未到位）"
            return True, str(ver)[:60]
        finally:
            await conn.close()

    try:
        return asyncio.run(_go())
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def _tcp_open(host: str, port: int, timeout: float = 3.0) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


_GATE_CACHE: dict[str, tuple[bool, str]] = {}


# --------------------------------------------------------------------------- #
# 2. 门控 fixture                                                              #
# --------------------------------------------------------------------------- #
@pytest.fixture
def realstack() -> dict[str, Any]:
    """真实栈门控。不满足条件就 skip，reason 必须能直接照着修。"""
    if not _gate_enabled():
        pytest.skip(
            f"真实栈测试未启用：请设置环境变量 {GATE_ENV_VAR}=1 "
            f"（当前值 {os.environ.get(GATE_ENV_VAR, '<unset>')!r}）。"
            "离线快测不应调用本机 Ollama / PostgreSQL。"
        )

    from forgeflow.config import get_settings

    settings = get_settings()
    base_url = str(getattr(settings, "ollama_base_url", "") or "http://localhost:11434")
    dsn = str(settings.postgres_url).replace("postgresql+asyncpg://", "postgresql://")

    if "ollama" not in _GATE_CACHE:
        _GATE_CACHE["ollama"] = _probe_ollama(base_url)
    ok, why = _GATE_CACHE["ollama"]
    if not ok:
        pytest.skip(f"Ollama 不可达（{base_url}）：{why}。请先启动本机 Ollama 服务。")

    if "pg" not in _GATE_CACHE:
        _GATE_CACHE["pg"] = _probe_pg(dsn)
    ok, why = _GATE_CACHE["pg"]
    if not ok:
        pytest.skip(f"PostgreSQL 不可达（{dsn.split('@')[-1]}）：{why}。请先启动容器。")

    return {"ollama_base_url": base_url, "pg_dsn": dsn}


@pytest.fixture
def realstack_env(realstack, monkeypatch) -> Any:
    """把进程切到**真实栈**配置：postgres 存储 + ollama 模型。

    ``tests/conftest.py`` 用 ``setdefault`` 把离线档钉成 memory/mock；这里显式
    改回真实值并清 ``get_settings`` 的 lru 缓存 + 所有 backend-keyed 单例，
    保证仓储/指标源/workspace store 都在 postgres 上重建。
    """
    from forgeflow.config import get_settings
    from forgeflow.observability.metrics_source import reset_metrics_source
    from forgeflow.repositories.factory import reset_repositories
    from forgeflow.workspace.store import reset_workspace_store

    monkeypatch.setenv("STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    get_settings.cache_clear()

    settings = get_settings()
    assert str(settings.storage_backend).lower() == "postgres", (
        f"真实栈用例必须跑在 postgres 上，实际 storage_backend={settings.storage_backend!r}"
    )
    assert str(settings.llm_provider).lower() == "ollama", (
        f"真实栈用例必须跑在 ollama 上，实际 llm_provider={settings.llm_provider!r}"
    )

    reset_repositories()
    reset_metrics_source()
    reset_workspace_store()

    yield settings

    reset_repositories()
    reset_metrics_source()
    reset_workspace_store()
    get_settings.cache_clear()


@pytest.fixture
async def pg_conn(realstack):
    """**独立**的 asyncpg 连接 —— 不复用应用自己的 pool / store。

    T2 的回查必须走这条独立通道：用应用自己的 store 读回来，只能证明
    "store 的 in-process 缓存里有"，证明不了"落到了 PostgreSQL"。
    """
    import asyncpg

    conn = await asyncpg.connect(realstack["pg_dsn"], timeout=8)
    try:
        yield conn
    finally:
        await conn.close()


# --------------------------------------------------------------------------- #
# 3. 真实跑一次 run_task（按 tag 进程内复用，避免重复烧 token）                  #
# --------------------------------------------------------------------------- #
async def drive_real_run(
    tag: str,
    *,
    intent: str,
    tenant: str,
    role: str = "admin",
    context: dict[str, Any] | None = None,
    ollama_base_url: str | None = None,
) -> dict[str, Any]:
    """真的驱动一次 ``run_task`` 并返回可断言的产物。

    Returns a dict with keys: ``handle`` / ``record`` / ``task`` / ``detail`` /
    ``usage``。``usage`` 是这次运行**真实**上报的 token 用量条目（来自 Ollama
    响应的 usage_metadata），是成本断言的唯一可信来源。
    """
    if tag in _REAL_RUNS:
        return _REAL_RUNS[tag]

    from forgeflow.runtime.orchestrator import (
        RequestContext,
        TaskCreate,
        get_run_store,
        run_task,
    )

    if ollama_base_url is not None:
        from forgeflow.config import get_settings
        from forgeflow.models import provider as _prov

        previous = str(get_settings().ollama_base_url)
        get_settings().ollama_base_url = ollama_base_url
        # 探活结果按 TTL 缓存；换端口后必须失效，否则会用旧结论。
        _prov._ollama_probe_cache["at"] = 0.0  # noqa: SLF001

    task = TaskCreate(
        intent=intent,
        workflow_type="generic",
        context=dict(context or {}),
    )
    try:
        handle = await run_task(
            task,
            RequestContext(tenant_id=tenant, user_id="u-realstack-qa", role=role),
        )
    finally:
        if ollama_base_url is not None:
            # 还原，并再次让探活缓存失效：否则 TTL 内其它用例会读到
            # "Ollama 不可达"的旧结论而静默降级。
            get_settings().ollama_base_url = previous
            _prov._ollama_probe_cache["at"] = 0.0  # noqa: SLF001
    record = get_run_store().get(handle.run_id)
    _CLEANUP_RUN_IDS.add(handle.run_id)
    payload = {
        "handle": handle,
        "record": record,
        "task": task,
        "detail": handle.detail,
        "usage": list(task.context.get("llm_usage") or []),
    }
    _REAL_RUNS[tag] = payload
    return payload


@pytest.fixture(scope="session", autouse=True)
def _purge_realstack_rows():
    """会话结束清掉本包真实跑出来的行（workspace_runs / experiences）。

    真实栈会往**共享的开发库**写数据；不清会让后续全量回归的计数断言
    （"Left contains N more items"）漂掉。
    """
    yield
    if not _CLEANUP_RUN_IDS or not _gate_enabled():
        return
    from forgeflow.config import get_settings

    dsn = str(get_settings().postgres_url).replace("postgresql+asyncpg://", "postgresql://")

    async def _go() -> None:
        import asyncpg

        try:
            conn = await asyncpg.connect(dsn, timeout=8)
        except Exception:  # noqa: BLE001 — 清理失败不该让测试变红
            return
        try:
            for run_id in sorted(_CLEANUP_RUN_IDS):
                await conn.execute("DELETE FROM workspace_runs WHERE run_id = $1", run_id)
                await conn.execute("DELETE FROM experiences WHERE run_id = $1", run_id)
        finally:
            await conn.close()

    try:
        asyncio.run(_go())
    except Exception:  # noqa: BLE001
        pass
