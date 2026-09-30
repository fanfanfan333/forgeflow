"""INC26 T04 — 代码执行面配置打通的 fail-closed 钉子 + `.env` 陷阱守卫。

设计 §0.1 / §7.3 的三条断言，全部是**机制**（不是措辞）：

1. **未配置 ⇒ fail-closed**：空解释器 ⇒ `SubprocessOpenHandsEngine.available()` 为
   `False`，且带逐字 `reason`（绝不内置默认解释器）。
2. **`.env` 陷阱守卫**：只在 `.env` 文件里写 `FORGEFLOW_CODEPLANE_PYTHON` ⇒ 仍
   `available() == False`。原因是 `pydantic-settings` 把 `.env` 读进 Settings 对象，
   **不会**把它注入 `os.environ`，而 `FORGEFLOW_CODEPLANE_PYTHON` 只是 `engine.py` 的
   `os.environ.get` 兜底 ⇒ 两条通道都不命中，静默降级。
3. **测试命令非裸 `python`**：`Settings.codeplane_test_command` 的首个 token 必须是绝对
   路径解释器（裸 `python` 在 Windows 上常是静默 exit 1 的 WindowsApps 存根）。

引文纪律：一律 `file.py::symbol`，不用行号。
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from forgeflow.codeplane.engine import _DEFAULT_RUNNER, SubprocessOpenHandsEngine

#: 本机两个 venv 的绝对解释器（实测存在；缺失时相关用例显式 skip）。
AGENTFLOW_PY = r"C:\Users\18769\.workbuddy\binaries\python\envs\agentflow\Scripts\python.exe"
OPENHANDS_PY = r"C:\Users\18769\.workbuddy\binaries\python\envs\openhands\Scripts\python.exe"

_BARE_PYTHON = {"python", "python.exe", "python3", "python3.exe", ""}


@pytest.fixture(autouse=True)
def _fresh_settings():
    """前后清空 `get_settings` 缓存，避免被 monkeypatch 的取值残留。"""
    from forgeflow.config import get_settings

    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


def _engine(interp: str) -> SubprocessOpenHandsEngine:
    """A real engine bound to a minimal settings stub (runner argv is real)."""
    settings = SimpleNamespace(codeplane_enabled=True, codeplane_interpreter=interp)
    return SubprocessOpenHandsEngine(settings=settings, runner_argv=[str(_DEFAULT_RUNNER)])


def _first_token(command: str) -> str:
    parts = (command or "").strip().split()
    return parts[0].strip("\"'") if parts else ""


# --------------------------------------------------------------------------- #
# 1. fail-closed：未配置 ⇒ False + 逐字 reason（不得内置默认解释器）          #
# --------------------------------------------------------------------------- #
def test_unconfigured_engine_is_fail_closed(monkeypatch):
    monkeypatch.delenv("FORGEFLOW_CODEPLANE_PYTHON", raising=False)
    engine = _engine("")
    assert engine.available() is False
    assert engine.interpreter == ""
    assert engine.reason, "fail-closed 未给出 reason"


def test_disabled_engine_is_fail_closed():
    settings = SimpleNamespace(codeplane_enabled=False, codeplane_interpreter=AGENTFLOW_PY)
    engine = SubprocessOpenHandsEngine(settings=settings, runner_argv=[str(_DEFAULT_RUNNER)])
    assert engine.available() is False
    assert "关闭" in engine.reason


# --------------------------------------------------------------------------- #
# 2. `.env` 陷阱守卫：写别名 ⇒ 仍不可用（不注入 os.environ）                    #
# --------------------------------------------------------------------------- #
def test_env_file_alias_is_a_silent_noop(tmp_path, monkeypatch):
    # 先确保进程环境里没有这个兜底名，才能证明"只在 .env 写"确实无效。
    monkeypatch.delenv("FORGEFLOW_CODEPLANE_PYTHON", raising=False)

    env_file = tmp_path / ".env"
    env_file.write_text(f"FORGEFLOW_CODEPLANE_PYTHON={OPENHANDS_PY}\n", encoding="utf-8")

    from forgeflow.config import Settings

    settings = Settings(_env_file=str(env_file))
    # 别名不是 Settings 字段 ⇒ 空（这里没有任何"等价别名"生效）。
    assert getattr(settings, "codeplane_interpreter", "") == ""
    # 铁证：pydantic-settings 不会把 .env 的值注入进程环境。
    assert os.environ.get("FORGEFLOW_CODEPLANE_PYTHON") is None

    engine = SubprocessOpenHandsEngine(
        settings=SimpleNamespace(
            codeplane_enabled=True,
            codeplane_interpreter=settings.codeplane_interpreter,
        ),
        runner_argv=[str(_DEFAULT_RUNNER)],
    )
    assert engine.available() is False, "只写 .env 别名竟让引擎可用 —— 陷阱守卫失败"


# --------------------------------------------------------------------------- #
# 3. 配置存在路径 ⇒ 可用（positive control，证明上面不是恒 False）            #
# --------------------------------------------------------------------------- #
def test_configured_existing_interpreter_is_available(monkeypatch):
    monkeypatch.delenv("FORGEFLOW_CODEPLANE_PYTHON", raising=False)
    if not Path(AGENTFLOW_PY).exists():
        pytest.skip("agentflow venv interpreter 不存在（跳过 positive control）")
    engine = _engine(AGENTFLOW_PY)
    assert engine.available() is True
    assert engine.interpreter == AGENTFLOW_PY


def test_missing_interpreter_path_is_unavailable():
    engine = _engine(str(Path("Z:/no/such/venv/python.exe")))
    assert engine.available() is False
    assert "不存在" in engine.reason


# --------------------------------------------------------------------------- #
# 4. 测试命令非裸 python 钉子（当前有效配置）                                  #
# --------------------------------------------------------------------------- #
def test_codeplane_test_command_is_not_bare_python():
    from forgeflow.config import get_settings

    command = get_settings().codeplane_test_command
    first = _first_token(command)
    assert first not in _BARE_PYTHON, f"测试命令首 token 是裸 python：{command!r}"
    assert (os.sep in first) or ("/" in first), (
        f"测试命令首 token 不是绝对路径解释器：{command!r}"
    )


def test_test_command_nail_goes_red_for_a_bare_python(monkeypatch):
    """反事实 C3 —— 把命令改回裸 python，钉子必须判定为红。"""
    from forgeflow.config import get_settings

    monkeypatch.setenv("CODEPLANE_TEST_COMMAND", "python -m pytest -q")
    get_settings.cache_clear()
    first = _first_token(get_settings().codeplane_test_command)
    # 钉子谓词："首 token 不是裸 python 且形如路径"。这里必须为假 ⇒ 钉子会变红。
    nail_passes = first not in _BARE_PYTHON and ((os.sep in first) or ("/" in first))
    assert nail_passes is False
