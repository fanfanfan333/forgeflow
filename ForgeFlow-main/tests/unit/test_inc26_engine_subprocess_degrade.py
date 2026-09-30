"""INC26 收口钉子 —— 代码执行面**与事件循环无关**（Proactor / Selector 行为一致）。

机制：``forgeflow/codeplane/engine.py::SubprocessOpenHandsEngine.run`` 用
``subprocess.Popen`` 起子进程，并把阻塞的 ``Popen.communicate`` 放进 ``asyncio.to_thread``。
这样启动子进程**不依赖事件循环的 subprocess transport** —— ``asyncio`` 的
``create_subprocess_exec`` 在 Windows 仅 Proactor 循环可用，而 psycopg3 异步 checkpointer
（``forgeflow/graph/checkpointer.py::get_checkpointer``）要求 Selector 循环；线程化后
**两种循环行为一致**，故代码执行面与 postgres 档可共用同一循环。

本文件断言的是**机制**（不是措辞）：

1. 真子进程产出真实 ``result`` 行 ⇒ ``ok``（positive control，任意循环）。
2. **在真正的 ``WindowsSelectorEventLoopPolicy`` 下**跑真子进程 ⇒ 仍 ``ok`` —— 这就是
   "机制与循环无关"的证明（旧的 ``create_subprocess_exec`` 实现在此必红）。非 Windows skip。
3. 超时 ⇒ ``degraded == "timeout"``，且子进程确已收掉（``started`` 有、``finished`` 无）。
4. 真实缺失解释器 ⇒ 已登记 ``engine_unavailable``（真 ``FileNotFoundError``）。

红线：只用 stdlib；``measured`` 语义不变（失败/超时仍是**未测量**，不写 ``0``）。
引文纪律：一律 ``file.py::symbol``，不用行号。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from forgeflow.codeplane.engine import (
    CodeJob,
    SubprocessOpenHandsEngine,
)
from forgeflow.codeplane.protocol import DEGRADED_VALUES

#: A real child that reads the job off stdin and prints one valid ``result`` line.
_RESULT_SCRIPT = """\
import sys, json
sys.stdin.read()
print(json.dumps({
    "seq": 0,
    "ts": "2026-01-01T00:00:00+00:00",
    "phase": "done",
    "kind": "result",
    "status": "ok",
    "data": {"exit_code": 0},
}), flush=True)
"""

#: A real child that proves it started, then sleeps past the wall timeout.
_SLEEP_SCRIPT = """\
import sys, time, pathlib

_d = pathlib.Path(sys.argv[1])
(_d / "started.txt").write_text("1", encoding="utf-8")
time.sleep(30)
(_d / "finished.txt").write_text("1", encoding="utf-8")
"""


def _engine(runner: Path, extra: list[str] | None = None) -> SubprocessOpenHandsEngine:
    """A real engine whose interpreter is this test's own python (always exists)."""
    settings = SimpleNamespace(
        codeplane_enabled=True,
        codeplane_interpreter=sys.executable,
        ollama_base_url="http://127.0.0.1:11434",
    )
    argv = [str(runner), *(extra or [])]
    return SubprocessOpenHandsEngine(settings=settings, runner_argv=argv)


def _job(wall: int) -> CodeJob:
    """A fully-populated job so ``_merge_defaults`` never needs extra settings."""
    return CodeJob(
        run_id="r-nail",
        task_intent="noop",
        workspace_path="",
        model="ollama_chat/qwen3:8b",
        base_url="http://127.0.0.1:11434",
        api_key="",
        max_rounds=1,
        wall_timeout_s=wall,
        test_command="python -m pytest -q",
        language_hint="python",
        reasoning_effort="none",
        num_ctx=1,
        temperature=0.0,
    )


def test_real_subprocess_result_line_is_ok(tmp_path):
    """Positive control — a real child emitting a real ``result`` line ⇒ ``ok``."""
    runner = tmp_path / "runner_ok.py"
    runner.write_text(_RESULT_SCRIPT, encoding="utf-8")
    engine = _engine(runner)
    assert engine.available() is True

    result = asyncio.run(engine.run(_job(wall=60)))

    assert result.status == "ok"
    assert result.degraded is None
    assert result.exit_code == 0


@pytest.mark.skipif(sys.platform != "win32", reason="Proactor/Selector 仅 Windows")
def test_loop_independent_under_windows_selector(tmp_path):
    """机制与循环无关：真实 Selector 循环下，真子进程仍得 ``ok``（旧实现必红）。"""
    runner = tmp_path / "runner_ok.py"
    runner.write_text(_RESULT_SCRIPT, encoding="utf-8")
    engine = _engine(runner)

    async def _drive():
        loop = asyncio.get_running_loop()
        # Windows 下真实类名是 ``_WindowsSelectorEventLoop``（``WindowsSelectorEventLoopPolicy``
        # 的产物），并非裸 ``SelectorEventLoop``——太严的类名断言会误红，故按前缀判定。
        assert type(loop).__name__.endswith("SelectorEventLoop"), (
            f"钉子要求真实 Selector 循环，实得 {type(loop).__name__}"
        )
        return await engine.run(_job(wall=60))

    saved = asyncio.get_event_loop_policy()
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        result = asyncio.run(_drive())
    finally:
        asyncio.set_event_loop_policy(saved)

    assert result.status == "ok"
    assert result.degraded is None


def test_timeout_kills_child_and_degrades(tmp_path):
    """Wall timeout ⇒ ``timeout`` degrade, and the child is really reaped."""
    runner = tmp_path / "runner_sleep.py"
    runner.write_text(_SLEEP_SCRIPT, encoding="utf-8")
    markers = tmp_path / "markers"
    markers.mkdir()
    engine = _engine(runner, extra=[str(markers)])

    result = asyncio.run(engine.run(_job(wall=3)))

    assert result.degraded == "timeout"
    assert result.status != "ok"
    assert result.tests.measured is False
    assert (markers / "started.txt").exists(), "子进程未真正启动（钉子前提失败）"
    assert not (markers / "finished.txt").exists(), "超时后子进程未被收掉"


def test_real_missing_interpreter_degrades_unavailable(tmp_path, monkeypatch):
    """A real missing executable ⇒ registered ``engine_unavailable`` (真 FileNotFoundError)."""
    runner = tmp_path / "runner_ok.py"
    runner.write_text(_RESULT_SCRIPT, encoding="utf-8")
    settings = SimpleNamespace(
        codeplane_enabled=True,
        codeplane_interpreter=str(tmp_path / "no_such_python.exe"),
        ollama_base_url="http://127.0.0.1:11434",
    )
    engine = SubprocessOpenHandsEngine(settings=settings, runner_argv=[str(runner)])
    # Bypass the availability gate so the test reaches the real ``Popen`` start.
    monkeypatch.setattr(engine, "available", lambda: True)

    result = asyncio.run(engine.run(_job(wall=5)))

    assert result.degraded == "engine_unavailable"
    assert result.degraded in DEGRADED_VALUES
    assert result.status != "ok"
    assert result.tests.measured is False
    assert result.timeline and result.timeline[0].detail.strip()
