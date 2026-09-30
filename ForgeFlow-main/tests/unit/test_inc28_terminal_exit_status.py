"""INC28 W8 —— 终端**结构化**退出码也必须能判「失败观测」。

## 留存经验（为什么要这条钉子）

W3 把「错误观测冒充『步骤已完成』」修成了三个信号（``observation.is_error`` /
``"❌"`` / ``ERROR_MESSAGE_HEADER`` 前缀）。但真实运行里仍有一条漏网：PowerShell 报
``pytest 不是可识别的命令``（``CommandNotFoundException``，退出码 1）时，SDK **没有**
把它标成 ``is_error``，于是时间线仍写「步骤已完成」——平台自己的诚实性主张被自己的
时间线打脸。

修法**不是**对输出文本做启发式（测试输出里就含 ``error`` / ``failed``，启发式会把
**成功**的测试步也判红），而是读 SDK 的**结构化**字段：``TerminalObservation`` 带
``exit_code: int | None`` 与 ``timeout: bool``
（``openhands/tools/terminal/definition.py``）。判错 **iff** ``timeout is True`` **或**
(``exit_code is not None and exit_code not in (0, -1)``)。

保守边界（**不得**判错，避免假红）：
* ``exit_code is None`` —— 本 SDK 明确可为 ``None`` = **未测量**，缺测量不是失败证据；
* ``exit_code == -1`` —— SDK 的**软超时/尚未结束**哨兵，「还在跑」不是「失败」，硬超时
  已由运行自身的 ``degraded="timeout"`` 如实上报，这里再判只会造出第二个假红。

## INC28 W9 —— ``AgentErrorEvent``（引擎侧错误）不得被渲染成成功

SDK 的 ``AgentErrorEvent``（``openhands/sdk/event/llm_convertible/observation.py``）
是 ``ObservationBaseEvent``，带必填 ``error: str`` 与 ``classification``，描述**由
agent/scaffold 产生的错误**。它原先落在 ``_emit_oh_event`` 的默认档
``("observation","other","ok","（未登记的引擎事件）")`` —— 真错误被渲染成 ``ok``。修法是
把它**与 ``ConversationErrorEvent`` 同款映射**（``phase="error"`` / ``kind="engine"`` /
``status="error"`` / 业务 label「引擎报告执行错误」），并让 ``error`` 文本只进 ``detail``。
``classification.kind == AGENT_ACTION``（预期内、agent 可自纠）**仍**是「未成功」，本轮
不分级，一律 ``status="error"``。

引文一律 ``file.py::symbol``，不用行号。
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

_RUNNER_PATH = (
    Path(__file__).resolve().parents[2]
    / "forgeflow"
    / "codeplane"
    / "runner"
    / "run_code_task.py"
)


def _load_runner():
    spec = importlib.util.spec_from_file_location("ff_runner_inc28_exit", _RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def runner():
    return _load_runner()


class _Obs:
    """Stand-in for ``TerminalObservation`` (same structured fields, no SDK)."""

    def __init__(
        self,
        *,
        is_error: bool = False,
        exit_code: int | None = None,
        timeout: bool = False,
        text: str = "",
    ) -> None:
        self.is_error = is_error
        self.exit_code = exit_code
        self.timeout = timeout
        self.text = text


class ObservationEvent:
    """Named to match the SDK event the runner maps (so kind stays ``observation``)."""

    def __init__(self, observation: _Obs, tool_name: str = "terminal") -> None:
        self.observation = observation
        self.tool_name = tool_name


def _event(obs: _Obs) -> ObservationEvent:
    return ObservationEvent(obs)


# --------------------------------------------------------------------------- #
# 1. 第 4 个（结构化）信号：exit_code / timeout                                  #
# --------------------------------------------------------------------------- #
def test_nonzero_exit_code_is_a_failure(runner):
    """`pytest` 不是命令：进程真跑了、退出码 1 ⇒ 判失败（核心修复）。"""
    evt = _event(_Obs(exit_code=1, text="pytest : not recognized"))
    assert runner._event_is_error(evt, evt.observation.text) is True


def test_zero_exit_code_is_not_a_failure(runner):
    """阳性对照：`3 passed`（exit_code=0）**绝不**判红。"""
    evt = _event(_Obs(exit_code=0, text="3 passed in 0.03s"))
    assert runner._event_is_error(evt, evt.observation.text) is False


def test_none_exit_code_is_not_judged(runner):
    """`exit_code is None` = 未测量 ⇒ 不改判（缺测量不是失败证据）。"""
    evt = _event(_Obs(exit_code=None, text="some output"))
    assert runner._event_is_error(evt, evt.observation.text) is False


def test_soft_timeout_sentinel_minus_one_is_not_judged(runner):
    """`exit_code == -1` = 软超时/尚未结束 ⇒ **不**判错，避免假红。"""
    evt = _event(_Obs(exit_code=-1, text="still running"))
    assert runner._event_is_error(evt, evt.observation.text) is False


def test_timeout_flag_is_a_failure(runner):
    """显式 `timeout=True` 是失败 —— 即使退出码为 0。"""
    evt = _event(_Obs(timeout=True, exit_code=0, text=""))
    assert runner._event_is_error(evt, evt.observation.text) is True


def test_exit_code_one_on_a_non_terminal_observation_is_ignored(runner):
    """非终端观测没有 exit_code 语义 ⇒ `None`，不被误读。"""
    evt = _event(_Obs(exit_code=None))
    assert runner._event_is_error(evt, "file_editor Here's the result") is False


# --------------------------------------------------------------------------- #
# 2. 反启发式：文本里的 error/failed **不**能触发判错（否则成功的测试步会变红）    #
# --------------------------------------------------------------------------- #
def test_text_containing_error_words_is_not_a_failure(runner):
    """`3 passed` 的输出里可能含 'error' 之类词；只要 exit_code=0 就不判错。"""
    noisy = "ERROR collecting nothing; 0 failed; 3 passed"
    evt = _event(_Obs(exit_code=0, text=noisy))
    assert runner._event_is_error(evt, noisy) is False


# --------------------------------------------------------------------------- #
# 3. 既有三个信号仍生效（回归保护，不得被 W8 覆盖掉）                            #
# --------------------------------------------------------------------------- #
def test_existing_signals_still_hold(runner):
    assert runner._event_is_error(_event(_Obs(is_error=True)), "") is True
    assert runner._event_is_error(_event(_Obs()), "❌ rejected") is True
    assert (
        runner._event_is_error(_event(_Obs()), runner._SDK_ERROR_HEADER + " boom")
        is True
    )


# --------------------------------------------------------------------------- #
# 4. 接线：命中后状态/label/详情/latency 的口径                                  #
# --------------------------------------------------------------------------- #
def _emit(runner, event) -> dict:
    stream = io.StringIO()
    runner._emit_oh_event(runner.Emitter(stream=stream), event)
    return json.loads(stream.getvalue().strip())


def test_emit_maps_terminal_failure_to_error_with_business_label(runner):
    raw = "pytest : The term 'pytest' is not recognized ... (CommandNotFoundException)"
    event = ObservationEvent(_Obs(exit_code=1, text=raw), tool_name="terminal")
    out = _emit(runner, event)

    assert out["status"] == "error"
    assert out["label"] == runner._ERROR_LABELS["observation"]  # 「步骤产出异常」
    # 原始工具输出只进 detail，绝不进可见 label（INC25 AC-13）。
    assert raw not in out["label"]
    assert raw in out["detail"]
    # 未测量 ⇒ 不写 latency_ms（绝不 0）。
    assert "latency_ms" not in out


def test_emit_keeps_a_green_terminal_step_ok(runner):
    event = ObservationEvent(_Obs(exit_code=0, text="3 passed in 0.03s"), tool_name="terminal")
    out = _emit(runner, event)

    assert out["status"] == "ok"
    assert out["label"] == "步骤已完成"
    assert "latency_ms" not in out


# --------------------------------------------------------------------------- #
# 5. INC28 W9 —— AgentErrorEvent（引擎侧错误）不得再落成「未登记的引擎事件」/ok     #
# --------------------------------------------------------------------------- #
class _Classification:
    """Stand-in for ``ErrorClassification`` (only ``kind`` is read here)."""

    def __init__(self, kind: str) -> None:
        self.kind = kind


class AgentErrorEvent:
    """Named to match the SDK event the runner maps (W9)."""

    def __init__(
        self,
        *,
        error: str = "",
        tool_name: str = "file_editor",
        classification: _Classification | None = None,
    ) -> None:
        self.error = error
        self.tool_name = tool_name
        self.classification = classification


def test_agent_error_event_with_text_is_an_error(runner):
    """有 `error` 文本：→ status="error" + 业务 label；error 原文只进 detail。"""
    raw = "openhands: old_str was not found (action execution raised)"
    event = AgentErrorEvent(error=raw, tool_name="file_editor")
    out = _emit(runner, event)

    assert out["status"] == "error"
    assert out["label"] == "引擎报告执行错误"
    assert out["label"] != "（未登记的引擎事件）"
    # 原文只进 detail，绝不进可见 label（INC25 AC-13）。
    assert raw not in out["label"]
    assert raw in out["detail"]
    assert "latency_ms" not in out


def test_agent_error_event_without_text_is_still_an_error(runner):
    """无 `error` 文本：仍是错误事件（有 error 只是补充，判错不依赖它）。"""
    event = AgentErrorEvent(error="", tool_name="terminal")
    out = _emit(runner, event)

    assert out["status"] == "error"
    assert out["label"] == "引擎报告执行错误"


def test_agent_error_event_with_agent_action_classification_is_still_an_error(runner):
    """`classification.kind == AGENT_ACTION`（预期内、可自纠）**仍**是「未成功」。

    本轮不分级：可自纠的校验失败也只是「没成功」，不得判成 success/ok。
    """
    event = AgentErrorEvent(
        error="validation failed: unknown command",
        tool_name="file_editor",
        classification=_Classification("AGENT_ACTION"),
    )
    out = _emit(runner, event)

    assert out["status"] == "error"
    assert out["label"] == "引擎报告执行错误"


def test_agent_error_event_is_mapped_like_conversation_error(runner):
    """与 `ConversationErrorEvent` 同一映射（同类事件同一口径）。"""
    out = _emit(runner, AgentErrorEvent(error="x"))
    assert (out["phase"], out["kind"], out["label"]) == (
        "error",
        "engine",
        "引擎报告执行错误",
    )

