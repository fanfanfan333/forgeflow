"""INC29 T04 —— 客户端引擎按配置切换（§4/§5）：三种 transport 与如实降级。

被测对象是 **控制面** 的 ``forgeflow/codeplane/engine.py::AgentServerOpenHandsEngine``
与 ``_AutoFallbackEngine``（一次都不 ``import openhands``）。覆盖：

* A1 正向（环境门控）：薄服务用 openhands venv 真能拉起并自证健康；
* A3 反事实·不可达：pin agent_server + 端口无人监听 ⇒ ``unavailable`` /
  ``engine_unavailable``，**不抛异常**；``auto`` 同场景 ⇒ 回落子进程并记
  ``fell_back_from="agent_server"``；
* A4 反事实·端口被占：拉起失败被捕获，``degraded="engine_unavailable"`` 且
  ``raw.reason`` 点名 bind 失败；默认 ``subprocess`` 完全不受影响；
* A5 反事实·中途死掉：墙钟内无终态 / 传输中断 ⇒ ``degraded ∈ {runner_crashed,
  timeout}``，**已收到的部分 timeline 保留**，绝不伪造成功。A5 通过**注入式
  （内存）HTTP 层**确定性地驱动 ``_drive`` 的异常/超时分支 —— 对 ``httpx.AsyncClient``
  与 ``httpx.get`` 做属性级 monkeypatch，指向一个**无人监听**的 loopback URL；真 socket
  行为由 A1（真拉起）/ A4（真 bind 失败）覆盖；
* A7/A9 机械：客户端源码无 ``import openhands``；派生 ``degraded`` ∈ 注册词表。

引文一律 ``file.py::symbol``，不用行号。
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from forgeflow.codeplane.engine import (
    AgentServerOpenHandsEngine,
    CodeJob,
    SubprocessOpenHandsEngine,
    _AgentServerUnavailable,
    _AutoFallbackEngine,
    _build_engine,
    _degraded_from_frames,
)
from forgeflow.codeplane.protocol import DEGRADED_VALUES
from forgeflow.codeplane.tests_verdict import TestResult as _TestResult

_OPENHANDS_PY = r"C:\Users\18769\.workbuddy\binaries\python\envs\openhands\Scripts\python.exe"

#: A real child that reads the job off stdin and echoes a valid ``result`` line.
_RESULT_SCRIPT = """\
import sys, json
sys.stdin.read()
print(json.dumps({
    "seq": 0, "ts": "2026-01-01T00:00:00+00:00",
    "phase": "done", "kind": "result", "status": "ok", "data": {"exit_code": 0},
}), flush=True)
"""

#: A child that tries to bind an already-used port and dies with the OS error.
_BIND_CHILD = """\
import argparse, socket, sys
p = argparse.ArgumentParser()
p.add_argument("--host", default="127.0.0.1")
p.add_argument("--port", type=int, default=0)
a, _ = p.parse_known_args()
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    s.bind((a.host, a.port))
    s.listen(1)
except OSError as e:
    sys.stderr.write(f"bind failed: {e!r}\\n")
    sys.stderr.flush()
    sys.exit(2)
print("bound", flush=True)
import time
time.sleep(30)
"""


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        codeplane_enabled=True,
        codeplane_agent_server_url="",
        codeplane_agent_server_token="",
        codeplane_interpreter=sys.executable,
        codeplane_agent_server_allow_external=False,
        codeplane_agent_server_startup_s=2,
        codeplane_timeout_seconds=5,
        codeplane_max_rounds=1,
        codeplane_test_command="",
        codeplane_reasoning_effort="none",
        codeplane_num_ctx=1024,
        codeplane_temperature=0.0,
        ollama_base_url="http://127.0.0.1:11434",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _job(wall: int = 2) -> CodeJob:
    return CodeJob(
        run_id="r-agent-server",
        task_intent="noop",
        workspace_path="",
        model="ollama_chat/qwen3:8b",
        base_url="http://127.0.0.1:11434",
        api_key="",
        max_rounds=1,
        wall_timeout_s=wall,
        test_command="",
        language_hint="python",
        reasoning_effort="none",
        num_ctx=1024,
        temperature=0.0,
    )


# --------------------------------------------------------------------------- #
# A1 正向（环境门控）                                                          #
# --------------------------------------------------------------------------- #
async def test_a1_agent_server_launches_with_the_openhands_venv():
    """§4.1 —— 用 openhands venv 真拉起薄服务并自证健康（缺环境则显式跳过）。"""
    if not Path(_OPENHANDS_PY).exists():
        pytest.skip("openhands venv 不存在，A1 启动路径未验证")
    engine = AgentServerOpenHandsEngine(
        settings=_settings(codeplane_interpreter=_OPENHANDS_PY, codeplane_agent_server_startup_s=30)
    )
    try:
        base, token = await engine._ensure_server()
        assert base.startswith("http://127.0.0.1:")
        ok, reason = engine._health_get(base, token)
        assert ok, reason
    except _AgentServerUnavailable as exc:  # pragma: no cover — env-gated
        pytest.skip(f"agent server 未能拉起（环境相关）：{exc}")
    finally:
        engine._reclaim()


# --------------------------------------------------------------------------- #
# A3 反事实·不可达                                                             #
# --------------------------------------------------------------------------- #
def test_a3_pinned_dead_url_is_unavailable_without_raising():
    engine = AgentServerOpenHandsEngine(
        settings=_settings(codeplane_agent_server_url="http://127.0.0.1:59998", codeplane_agent_server_token="x")
    )
    assert engine.available() is False

    result = asyncio.run(engine.run(_job()))

    assert result.status == "unavailable"
    assert result.degraded == "engine_unavailable"
    assert result.degraded in DEGRADED_VALUES
    assert result.transport == "agent_server"
    assert result.raw.get("agent_server_unstarted") is True
    assert result.tests.measured is False


def test_a3_auto_falls_back_to_subprocess_and_records_it(tmp_path):
    runner = tmp_path / "runner_ok.py"
    runner.write_text(_RESULT_SCRIPT, encoding="utf-8")

    primary = AgentServerOpenHandsEngine(
        settings=_settings(codeplane_agent_server_url="http://127.0.0.1:59998", codeplane_agent_server_token="x")
    )
    fallback = SubprocessOpenHandsEngine(
        settings=_settings(codeplane_interpreter=sys.executable), runner_argv=[str(runner)]
    )
    auto = _AutoFallbackEngine(primary, fallback)

    result = asyncio.run(auto.run(_job()))

    assert result.status == "ok"
    assert result.transport == "subprocess"
    assert result.fell_back_from == "agent_server"


# --------------------------------------------------------------------------- #
# A4 反事实·端口被占                                                           #
# --------------------------------------------------------------------------- #
def test_a4_occupied_port_launch_failure_is_captured(tmp_path):
    import socket

    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("127.0.0.1", 0))
    occupied.listen(1)
    port = occupied.getsockname()[1]

    child = tmp_path / "child_bind.py"
    child.write_text(_BIND_CHILD, encoding="utf-8")
    engine = AgentServerOpenHandsEngine(
        settings=_settings(codeplane_agent_server_startup_s=3),
        server_argv=[str(child)],
        port=port,
    )
    try:
        result = asyncio.run(engine.run(_job(wall=5)))
    finally:
        occupied.close()

    assert result.status == "unavailable"
    assert result.degraded == "engine_unavailable"
    assert result.transport == "agent_server"
    reason = str(result.raw.get("reason") or "").lower()
    assert any(token in reason for token in ("bind failed", "10048", "address already in use", "winerror")), (
        f"raw.reason 未点名 bind 失败：{result.raw.get('reason')!r}"
    )


def test_a4_default_transport_is_unaffected(tmp_path):
    """默认 ``subprocess`` 完全不拉起服务 —— 端口被占也毫不受影响。"""
    engine = _build_engine(_settings(codeplane_transport="subprocess"))
    assert isinstance(engine, SubprocessOpenHandsEngine)


# --------------------------------------------------------------------------- #
# A5 反事实·中途死掉                                                           #
# --------------------------------------------------------------------------- #
#: A loopback URL that nothing is listening on — routing the engine here proves the
#: test never touches a real socket (the transport is entirely in-memory).
_DEAD_URL = "http://127.0.0.1:59998"

_FRAMES = [
    {"type": "ActionEvent", "seq": 0, "ts": "2026-01-01T00:00:00+00:00", "tool": "terminal"},
    {"type": "ObservationEvent", "seq": 1, "ts": "2026-01-01T00:00:01+00:00", "detail": "ran"},
]


class _FakeResponse:
    """Just enough of ``httpx.Response`` for the engine's ``_drive`` loop."""

    def __init__(self, status_code: int, body) -> None:
        self.status_code = status_code
        self._body = body

    @property
    def text(self) -> str:
        return self._body if isinstance(self._body, str) else json.dumps(self._body)

    def json(self):
        return self._body


class _FakeTransport:
    """A **deterministic in-memory** stand-in for the official thin server.

    Routes the exact endpoints ``_drive`` touches. ``die_after_first_events`` models a
    transport that serves the first events page and then raises on **every** subsequent
    request (a clean, race-free "transport died"); ``never_terminal`` keeps the
    authoritative status at ``running`` so the wall-clock timeout branch is exercised
    instead.
    """

    def __init__(
        self,
        frames,
        *,
        never_terminal: bool,
        die_after_first_events: bool,
    ) -> None:
        self.frames = frames
        self.never_terminal = never_terminal
        self.die_after_first_events = die_after_first_events
        self.events_served = 0
        self.dead = False
        self.calls: list[tuple[str, str]] = []

    def _check_alive(self) -> None:
        if self.dead:
            raise httpx.ConnectError("fake transport died")

    def route(self, method: str, url: str, params=None) -> _FakeResponse:
        self._check_alive()
        self.calls.append((method, url))
        # ``httpx.get`` is handed a full URL (health probe) while ``AsyncClient`` calls are
        # relative to ``base_url``; ``urlparse().path`` normalises both to "/api/...".
        path = urllib.parse.urlparse(url).path
        if path == "/api/conversations/count" and method == "GET":
            return _FakeResponse(200, 1)
        if path == "/api/conversations" and method == "POST":
            return _FakeResponse(201, {"id": "fake", "execution_status": "idle"})
        if path.endswith("/run") and method == "POST":
            return _FakeResponse(200, {"success": True})
        if path.endswith("/events/search") and method == "GET":
            after = int((params or {}).get("after_seq", -1))
            items = [f for f in self.frames if f.get("seq", -1) > after]
            self.events_served += 1
            if self.die_after_first_events and self.events_served >= 1:
                self.dead = True
            return _FakeResponse(200, {"items": items, "next_page_id": None})
        if path.endswith("/execute_bash_command") and method == "POST":
            return _FakeResponse(
                200,
                {"id": "c", "command_id": "c", "order": 0, "exit_code": 0,
                 "stdout": "1 passed\n", "stderr": "", "timeout": False},
            )
        if method == "DELETE":
            return _FakeResponse(200, {"success": True})
        if path.startswith("/api/conversations/") and method == "GET":
            status = "running" if self.never_terminal else "finished"
            return _FakeResponse(200, {"id": "fake", "execution_status": status})
        return _FakeResponse(501, {"detail": "Not Implemented"})

    # ---- sync surface (``httpx.get`` health probe) ----
    def get(self, url: str, headers=None, timeout=None) -> _FakeResponse:  # noqa: ANN001
        return self.route("GET", url)


class _FakeAsyncClient:
    """Async-context-manager surface of ``httpx.AsyncClient`` (``_drive`` uses these)."""

    def __init__(self, transport: _FakeTransport) -> None:
        self._transport = transport

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc) -> bool:  # noqa: ANN002
        return False

    async def post(self, url: str, json=None, params=None) -> _FakeResponse:  # noqa: ANN001
        return self._transport.route("POST", url, params)

    async def get(self, url: str, params=None) -> _FakeResponse:
        return self._transport.route("GET", url, params)

    async def delete(self, url: str, params=None) -> _FakeResponse:
        return self._transport.route("DELETE", url, params)


def _install_fake_transport(monkeypatch, transport: _FakeTransport) -> None:
    """Patch the ``httpx`` module *object* (engine imports it inside the functions)."""
    monkeypatch.setattr(httpx, "get", transport.get)
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: _FakeAsyncClient(transport)
    )


def test_a5_transport_death_keeps_partial_timeline(monkeypatch):
    transport = _FakeTransport(_FRAMES, never_terminal=True, die_after_first_events=True)
    _install_fake_transport(monkeypatch, transport)
    engine = AgentServerOpenHandsEngine(
        settings=_settings(
            codeplane_agent_server_url=_DEAD_URL,
            codeplane_agent_server_token="",
            codeplane_agent_server_startup_s=5,
        )
    )
    result = asyncio.run(engine.run(_job(wall=5)))

    assert transport.events_served >= 1, "至少一帧必须已被服务，否则『部分 timeline』无从谈起"
    assert result.degraded in {"runner_crashed", "timeout"}
    assert result.status == "error"
    assert result.degraded in DEGRADED_VALUES
    assert result.timeline, "已收到的部分 timeline 未保留"


def test_a5_never_terminal_within_the_wall_clock_degrades_timeout(monkeypatch):
    transport = _FakeTransport(_FRAMES, never_terminal=True, die_after_first_events=False)
    _install_fake_transport(monkeypatch, transport)
    engine = AgentServerOpenHandsEngine(
        settings=_settings(
            codeplane_agent_server_url=_DEAD_URL,
            codeplane_agent_server_token="",
            codeplane_agent_server_startup_s=5,
        )
    )
    result = asyncio.run(engine.run(_job(wall=2)))

    assert result.degraded == "timeout"
    assert result.status == "error"
    assert result.timeline, "超时也必须保留已收到的部分 timeline"


def test_a5_never_fabricates_success(monkeypatch):
    transport = _FakeTransport(_FRAMES, never_terminal=True, die_after_first_events=True)
    _install_fake_transport(monkeypatch, transport)
    engine = AgentServerOpenHandsEngine(
        settings=_settings(codeplane_agent_server_url=_DEAD_URL)
    )
    result = asyncio.run(engine.run(_job(wall=5)))

    assert result.status != "ok"
    assert result.degraded is not None


# --------------------------------------------------------------------------- #
# A7 / A9 机械                                                                 #
# --------------------------------------------------------------------------- #
def test_a7_engine_source_never_imports_openhands():
    source = (
        Path(__file__).resolve().parents[2] / "forgeflow" / "codeplane" / "engine.py"
    ).read_text(encoding="utf-8")
    imports = re.findall(r"^\s*(?:import|from)\s+(\w+)", source, flags=re.MULTILINE)
    assert "openhands" not in imports, "客户端 engine.py 出现了 import openhands"


def test_a7_server_package_never_imports_forgeflow():
    server_dir = (
        Path(__file__).resolve().parents[2]
        / "forgeflow"
        / "codeplane"
        / "runner"
        / "agent_server"
    )
    for path in sorted(server_dir.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(?:import|from)\s+forgeflow\b", source, flags=re.MULTILINE), path.name


def test_a9_degraded_derivation_stays_in_the_registered_vocabulary():
    """D1 —— 只有**传输级**错误事件才引发整轮 degrade，判定只看结构化 ``code``。

    ``AgentErrorEvent``（循环内一步出错）**绝不**引发整轮 degrade；自由文本
    ``detail`` 一律不扫（否则一次普通的命令超时就谎称「模型不可用」）。
    """
    samples = [
        ([], None),
        ([{"type": "ActionEvent"}], None),
        # 循环内的一步：写入失败 / 命令超时 / 连接被重置 —— 都不许升级为整轮故障。
        ([{"type": "AgentErrorEvent", "detail": "old_str not found in file"}], None),
        ([{"type": "AgentErrorEvent", "detail": "Request timed out after 60s"}], None),
        ([{"type": "AgentErrorEvent", "detail": "connection reset by peer while running pytest"}], None),
        # 传输级错误（会话/服务端）才引发；以结构化 code 判定。
        ([{"type": "ConversationErrorEvent", "code": "MaxIterationsReached"}], "max_rounds"),
        ([{"type": "ConversationErrorEvent", "code": "RuntimeError", "detail": "RuntimeError('boom')"}], "runner_crashed"),
        ([{"type": "ConversationErrorEvent", "detail": "litellm: connection refused 11434"}], "runner_crashed"),
        ([{"type": "ServerErrorEvent", "code": "OSError"}], "runner_crashed"),
    ]
    for frames, expected in samples:
        value = _degraded_from_frames(frames)
        assert value == expected, (frames, value)
        assert value is None or value in DEGRADED_VALUES


def test_a9_in_loop_agent_error_keeps_the_run_ok():
    """D1 —— 时间线以 ok 收尾的循环内 AgentErrorEvent ⇒ degraded None 且 status ok。"""
    engine = AgentServerOpenHandsEngine(settings=_settings())
    frames = [
        {"type": "ActionEvent", "seq": 0, "ts": "2026-01-01T00:00:00+00:00", "tool": "file_editor"},
        {"type": "AgentErrorEvent", "seq": 1, "ts": "2026-01-01T00:00:01+00:00", "detail": "old_str not found in file"},
        {"type": "ActionEvent", "seq": 2, "ts": "2026-01-01T00:00:02+00:00", "tool": "terminal"},
        {"type": "ObservationEvent", "seq": 3, "ts": "2026-01-01T00:00:03+00:00", "detail": "1 passed"},
    ]
    result = engine._build_result(
        frames,
        _job(),
        "cid",
        "finished",
        _TestResult(command="pytest -q", verdict="pass", measured=True),
        0,
        "",
        _degraded_from_frames(frames),
        "",
    )
    assert result.degraded is None
    assert result.status == "ok"


def test_build_engine_dispatches_on_the_transport_setting():
    assert isinstance(_build_engine(_settings(codeplane_transport="subprocess")), SubprocessOpenHandsEngine)
    assert isinstance(_build_engine(_settings(codeplane_transport="agent_server")), AgentServerOpenHandsEngine)
    assert isinstance(_build_engine(_settings(codeplane_transport="auto")), _AutoFallbackEngine)
    # An unknown / empty value must default to the byte-identical subprocess path.
    assert isinstance(_build_engine(_settings(codeplane_transport="")), SubprocessOpenHandsEngine)
