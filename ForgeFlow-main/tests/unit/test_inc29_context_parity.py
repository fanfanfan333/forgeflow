"""INC29 T04 review D2 —— 平台选定的注入段在两个 transport 上必须同源，且真的到达模型。

背景：``runtime/tool_handlers.py`` 把 ForgeFlow 选定的 skill/memory 塞进 ``CodeJob``，
``codeplane.injected`` 审计字段就由此渲染。子进程档（``run_code_task.py``）把它
append 进 system prompt；而 agent_server 档一度**只发 ``llm``/``tools``**，丢弃了这段
上下文，审计里却照样列出「已注入」——报告了一次从未发生的注入。

本文件钉三件事：

  1. **跨档同源**：同一 payload，runner 侧与 server 侧渲染出的块**逐字节相等**；空
     list 两档都返回 ``""``（无 header）。
  2. **客户端真的发出去了**：``AgentServerOpenHandsEngine._start_payload`` 的 ``agent``
     携带 ``skill_context`` / ``memory_context``（与 job 相等，原始 list 透传）。
  3. **服务端真的进了 system prompt**：``ConversationRuntime._build`` 建出的 ``Agent``
     的 system prompt **以最小提示词开头**且**包含**该技能行；无上下文时**不含**任何
     header（用假 SDK 记录 ``Agent(system_prompt=...)``，无需真 openhands 运行时）。

引文一律 ``file.py::symbol``。
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from forgeflow.codeplane.engine import AgentServerOpenHandsEngine, CodeJob
from forgeflow.codeplane.runner.agent_server import conversation

_RUNNER_PATH = (
    Path(__file__).resolve().parents[2]
    / "forgeflow"
    / "codeplane"
    / "runner"
    / "run_code_task.py"
)

_SKILL = {
    "id": "skill-fix-failing-test",
    "name": "修复失败测试",
    "version": "1.2.0",
    "description": "定位失败原因后改代码并重跑测试",
    "steps": ["读测试", "改代码", "跑 pytest"],
}
_MEMORY = {
    "id": "mem-8f3a",
    "content": "本模块的测试必须用 pytest -q 运行",
    "scope": "repo",
    "similarity": 0.91,
}


def _load_runner():
    """按路径加载 runner（它在 openhands venv 的 import 空间，模块级只用 stdlib）。"""
    spec = importlib.util.spec_from_file_location("ff_runner_inc29_d2", _RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def runner():
    return _load_runner()


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


def _job(**overrides) -> CodeJob:
    base = dict(
        run_id="r-d2",
        task_intent="noop",
        workspace_path="",
        model="ollama_chat/qwen3:8b",
        base_url="http://127.0.0.1:11434",
        api_key="",
        max_rounds=1,
        wall_timeout_s=5,
        test_command="",
        language_hint="python",
        reasoning_effort="none",
        num_ctx=1024,
        temperature=0.0,
        skill_context=[dict(_SKILL)],
        memory_context=[dict(_MEMORY)],
    )
    base.update(overrides)
    return CodeJob(**base)


# --------------------------------------------------------------------------- #
# 1) 跨档同源                                                                  #
# --------------------------------------------------------------------------- #
def test_runner_and_server_render_the_same_block_byte_for_byte(runner):
    """两个 transport 追加到 prompt 的**同一个块**必须逐字节相等。"""
    spec = {"skill_context": [dict(_SKILL)], "memory_context": [dict(_MEMORY)]}

    runner_block = runner._context_block(dict(spec))
    assert runner_block, "非空上下文必须渲染出内容"

    server_prompt = conversation._system_prompt_for(dict(spec))
    assert server_prompt.startswith(conversation._DEFAULT_SYSTEM_PROMPT), (
        "最小提示词必须仍是前缀（承重，不可被替换）"
    )
    server_block = server_prompt[len(conversation._DEFAULT_SYSTEM_PROMPT):]

    assert runner_block == server_block, "runner 与 server 渲染出的块必须逐字节相等"


def test_empty_context_renders_nothing_on_both_transports(runner):
    """无注入 ⇒ 无块、无 header（诚实缺省）。"""
    assert runner._context_block({}) == ""
    assert runner._context_block({"skill_context": [], "memory_context": []}) == ""

    assert conversation._system_prompt_for({}) == conversation._DEFAULT_SYSTEM_PROMPT
    assert (
        conversation._system_prompt_for({"skill_context": [], "memory_context": []})
        == conversation._DEFAULT_SYSTEM_PROMPT
    )


# --------------------------------------------------------------------------- #
# 2) 客户端真的发出去了                                                        #
# --------------------------------------------------------------------------- #
def test_start_payload_carries_the_selected_context():
    """``_start_payload`` 的 ``agent`` 必须携带原始 skill/memory 选中集。"""
    engine = AgentServerOpenHandsEngine(settings=_settings())
    job = _job()

    agent = engine._start_payload(job)["agent"]

    assert agent["skill_context"] == list(job.skill_context)
    assert agent["memory_context"] == list(job.memory_context)
    # 原始 list 透传：客户端不做任何渲染（渲染只发生在共享渲染器里）。
    assert "skills selected" not in str(agent).lower()


# --------------------------------------------------------------------------- #
# 3) 服务端真的进了 system prompt                                              #
# --------------------------------------------------------------------------- #
class _FakeOpenHands:
    """装一个假的 openhands SDK，捕获 ``Agent(system_prompt=...)``，无需真 SDK。"""

    def __init__(self) -> None:
        self.agent: object | None = None

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        created = self
        sdk = types.ModuleType("openhands.sdk")

        class _LLM:
            def __init__(self, **kwargs) -> None:  # noqa: ANN003
                self.kwargs = kwargs

        class _Agent:
            def __init__(self, llm=None, tools=None, system_prompt=None, **kwargs) -> None:  # noqa: ANN001
                self.llm = llm
                self.tools = tools
                self.system_prompt = system_prompt if system_prompt is not None else ""
                created.agent = self

        class _Conversation:
            def __init__(self, **kwargs) -> None:  # noqa: ANN003
                self.kwargs = kwargs
                self.state = types.SimpleNamespace(events=[])

            def run(self) -> None:  # pragma: no cover — never driven here
                pass

            def send_message(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003  # pragma: no cover
                pass

        sdk.LLM = _LLM
        sdk.Agent = _Agent
        sdk.Conversation = _Conversation
        monkeypatch.setitem(sys.modules, "openhands", types.ModuleType("openhands"))
        monkeypatch.setitem(sys.modules, "openhands.sdk", sdk)
        monkeypatch.setitem(sys.modules, "openhands.tools", types.ModuleType("openhands.tools"))


def _request(agent_spec: dict) -> SimpleNamespace:
    return SimpleNamespace(
        agent=agent_spec,
        workspace={"working_dir": ""},
        max_iterations=0,
        initial_message=None,
    )


def test_build_appends_the_context_to_the_agent_system_prompt(monkeypatch):
    """``_build`` 建出的 ``Agent`` 的 system prompt 必须含选定的 skill/memory。"""
    fake = _FakeOpenHands()
    fake.install(monkeypatch)

    spec = {
        "llm": {"model": "ollama_chat/qwen3:8b"},
        "tools": ["terminal"],
        "skill_context": [dict(_SKILL)],
        "memory_context": [dict(_MEMORY)],
    }
    conversation.ConversationRuntime("cid-d2", _request(spec))._build()

    prompt = fake.agent.system_prompt
    assert prompt.startswith(conversation._DEFAULT_SYSTEM_PROMPT), "最小提示词必须仍是前缀"
    assert _SKILL["name"] in prompt, "选定的技能必须真的进了 system prompt"
    assert _MEMORY["content"] in prompt, "选定的记忆必须真的进了 system prompt"


def test_build_without_context_adds_no_header(monkeypatch):
    """无上下文 ⇒ system prompt 恰好等于最小提示词（不含任何 header）。"""
    fake = _FakeOpenHands()
    fake.install(monkeypatch)

    spec = {"llm": {"model": "ollama_chat/qwen3:8b"}, "tools": ["terminal"]}
    conversation.ConversationRuntime("cid-d2", _request(spec))._build()

    assert fake.agent.system_prompt == conversation._DEFAULT_SYSTEM_PROMPT
    assert "Platform context" not in fake.agent.system_prompt
