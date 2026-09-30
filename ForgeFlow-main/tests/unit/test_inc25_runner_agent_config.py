"""INC25 runner self-proof: the code agent is built so local qwen actually acts.

P0 root cause (team-lead, controlled ablation): ``run_code_task.py`` built the
OpenHands ``Agent`` with (a) the SDK-default system prompt → the model treats the
task as Q&A (``status=STUCK``, 0 tool calls); (b) the SDK-default tool set, which
adds ``task_tracker`` → the model wanders off-task; and (c) no
``reasoning_effort`` → LiteLLM maps the default ``"high"`` onto Ollama
``think=True``, so the first round returns an EMPTY response.

P0-C (this file, additions): the local ``qwen3:8b`` is also **unstable at its
native temperature**, whose failure shape is a terminal soft-timeout retry loop
(``~201s``, 15 retries) that never finishes. The fix pins ``temperature=0.0`` and
adds two hard ceilings (round budget + wall clock), each reported **verbatim** so
a truncated run can never masquerade as a success.

Fix lives in ``forgeflow/codeplane/runner/run_code_task.py`` (a standalone program
that must never import ``forgeflow``) and ``forgeflow/codeplane/engine.py`` +
``forgeflow/config.py`` for the knobs. These tests inject a *fake* ``openhands``
SDK that records exactly what the runner passed to ``LLM(...)`` / ``Agent(...)`` —
no real SDK, no network — and assert each guard with a counterfactual that must go
RED.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import types
from pathlib import Path

import pytest

_RUNNER = (
    Path(__file__).resolve().parents[2]
    / "forgeflow"
    / "codeplane"
    / "runner"
    / "run_code_task.py"
)


class _FakeTool:
    def __init__(self, name, params=None):
        self.name = name
        self.params = dict(params or {})


class _FakeLLM:
    def __init__(self, **kw):
        _CAPTURED["llm"] = kw


class _FakeAgent:
    def __init__(self, **kw):
        _CAPTURED["agent"] = kw


# --- Event shapes the relay reacts to (names must match the SDK's) ----------- #
class ActionEvent:
    """One agent action (the relay counts these against the round budget)."""

    def __init__(self, idx):
        self.id = f"act-{idx}"
        self.tool_name = "terminal"


class ConversationErrorEvent:
    """The SDK's own native ``max_iteration_per_run`` reached signal."""

    def __init__(self):
        self.id = "max-iterations"
        self.code = "MaxIterationsReached"
        self.source = "environment"


#: Test-controlled behaviour for ``_FakeConversation.run()``:
#:   actions        — how many ``ActionEvent``s to push through the callbacks
#:   max_iterations — also push the native ``ConversationErrorEvent``
#:   sleep          — block for N seconds (drives the wall-clock guard)
_RUN_PLAN: dict = {}


class _FakeConversation:
    def __init__(self, agent=None, workspace=None, callbacks=None, max_iteration_per_run=None):
        self.callbacks = list(callbacks or [])
        self.state = types.SimpleNamespace(events=[])
        self.sent = None
        self.max_iteration_per_run = max_iteration_per_run

    def send_message(self, message):
        self.sent = message

    def run(self):
        for i in range(int(_RUN_PLAN.get("actions", 0))):
            for cb in self.callbacks:
                cb(ActionEvent(i))
        if _RUN_PLAN.get("max_iterations"):
            for cb in self.callbacks:
                cb(ConversationErrorEvent())
        if _RUN_PLAN.get("sleep"):
            import time as _time

            _time.sleep(float(_RUN_PLAN["sleep"]))
        return None


class _TerminalTool:
    name = "terminal"


class _FileEditorTool:
    name = "file_editor"


_CAPTURED: dict = {}

_FAKE_MODULES = (
    "openhands",
    "openhands.sdk",
    "openhands.tools",
    "openhands.tools.file_editor",
    "openhands.tools.terminal",
    "openhands.tools.preset",
    "openhands.tools.preset.default",
)


def _install_fake_openhands() -> None:
    sdk = types.ModuleType("openhands.sdk")
    sdk.LLM, sdk.Agent, sdk.Conversation, sdk.Tool = (
        _FakeLLM,
        _FakeAgent,
        _FakeConversation,
        _FakeTool,
    )
    file_editor = types.ModuleType("openhands.tools.file_editor")
    file_editor.FileEditorTool = _FileEditorTool
    terminal = types.ModuleType("openhands.tools.terminal")
    terminal.TerminalTool = _TerminalTool
    preset = types.ModuleType("openhands.tools.preset")
    default = types.ModuleType("openhands.tools.preset.default")
    default.get_default_tools = lambda *a, **k: [
        _FakeTool("terminal"),
        _FakeTool("file_editor"),
        _FakeTool("task_tracker"),
    ]
    sys.modules.update(
        {
            "openhands": types.ModuleType("openhands"),
            "openhands.sdk": sdk,
            "openhands.tools": types.ModuleType("openhands.tools"),
            "openhands.tools.file_editor": file_editor,
            "openhands.tools.terminal": terminal,
            "openhands.tools.preset": preset,
            "openhands.tools.preset.default": default,
        }
    )


@pytest.fixture
def runner():
    """Load the runner module against a fake SDK and restore global state after."""
    saved_modules = {name: sys.modules.get(name) for name in _FAKE_MODULES}
    _install_fake_openhands()
    spec = importlib.util.spec_from_file_location("run_code_task_under_test", _RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _CAPTURED.clear()
    _RUN_PLAN.clear()
    try:
        yield module
    finally:
        _RUN_PLAN.clear()
        for name, original in saved_modules.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


def _drive_conversation(module, workspace, job_extra=None):
    """Run the real ``_run_conversation`` once; return what was constructed."""
    _CAPTURED.clear()
    job = {
        "task_intent": "fix the failing test",
        "model": "ollama_chat/qwen3:8b",
        "base_url": "http://127.0.0.1:11434",
        "reasoning_effort": "none",
        "num_ctx": 32768,
    }
    if job_extra:
        job.update(job_extra)
    old_out = sys.stdout
    sys.stdout = io.StringIO()
    try:
        module._run_conversation(module.Emitter(sys.stdout), job, str(workspace))
    finally:
        sys.stdout = old_out
    return dict(_CAPTURED)


def _events_from(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# Guards A/B/C — the original P0 fix                                            #
# --------------------------------------------------------------------------- #
def test_llm_gets_reasoning_effort_none_and_num_ctx(runner, tmp_path):
    """Guard A — the LLM is built with the knobs that stop the empty first round."""
    captured = _drive_conversation(runner, tmp_path)
    llm_kw = captured["llm"]
    assert llm_kw["reasoning_effort"] == "none"
    assert llm_kw["litellm_extra_body"]["num_ctx"] == 32768


def test_agent_tools_are_exactly_terminal_and_file_editor(runner, tmp_path):
    """Guard B — the default preset's ``task_tracker`` is NOT handed to the agent."""
    captured = _drive_conversation(runner, tmp_path)
    names = {getattr(t, "name", None) for t in captured["agent"]["tools"]}
    assert names == {"terminal", "file_editor"}
    assert "task_tracker" not in names


def test_agent_gets_non_empty_custom_system_prompt(runner, tmp_path):
    """Guard C — a custom system prompt overrides OpenHands' built-in one."""
    captured = _drive_conversation(runner, tmp_path)
    prompt = captured["agent"]["system_prompt"]
    assert isinstance(prompt, str)
    assert prompt.strip()
    assert "ONLY by calling tools" in prompt


def test_counterfactual_reverting_all_three_turns_guards_red(runner, tmp_path):
    """Guard D — reverting the three places makes A/B/C all fail (they're load-bearing)."""
    original = (runner._llm_kwargs, runner._explicit_tools, runner._SYSTEM_PROMPT)
    try:
        runner._llm_kwargs = lambda job, model, base_url, api_key: {
            "model": model,
            "base_url": base_url,
        }
        # INC27: the seam now takes (workspace_path, emitter) — the fake accepts
        # the new signature so it still stands in for the real tool builder.
        runner._explicit_tools = lambda *args, **kwargs: [
            _FakeTool("terminal"),
            _FakeTool("file_editor"),
            _FakeTool("task_tracker"),
        ]
        runner._SYSTEM_PROMPT = ""
        captured = _drive_conversation(runner, tmp_path)
    finally:
        runner._llm_kwargs, runner._explicit_tools, runner._SYSTEM_PROMPT = original

    assert "reasoning_effort" not in captured["llm"]
    assert "temperature" not in captured["llm"]
    assert {getattr(t, "name", None) for t in captured["agent"]["tools"]} != {
        "terminal",
        "file_editor",
    }
    assert not captured["agent"]["system_prompt"]


# --------------------------------------------------------------------------- #
# P0-C (1) — temperature reaches LLM(...)                                        #
# --------------------------------------------------------------------------- #
def test_llm_gets_the_configured_temperature(runner, tmp_path):
    """The temperature knob travels all the way to ``LLM(...)``."""
    captured = _drive_conversation(runner, tmp_path, {"temperature": 0.0})
    assert captured["llm"]["temperature"] == 0.0


def test_llm_temperature_defaults_to_zero_when_absent(runner, tmp_path):
    """Counterfactual — a job WITHOUT ``temperature`` still pins 0.0 (never the
    unstable model default). Remove the default and this goes RED."""
    captured = _drive_conversation(runner, tmp_path)  # no temperature key
    assert captured["llm"]["temperature"] == 0.0

    # And the default is load-bearing: a reverted seam would omit the key entirely.
    original = runner._llm_kwargs
    try:
        runner._llm_kwargs = lambda job, model, base_url, api_key: {"model": model}
        reverted = _drive_conversation(runner, tmp_path)
    finally:
        runner._llm_kwargs = original
    assert "temperature" not in reverted["llm"]


# --------------------------------------------------------------------------- #
# P0-C (2) — the round budget really stops the agent, reported verbatim          #
# --------------------------------------------------------------------------- #
def test_max_rounds_stops_the_run_and_is_reported_verbatim(runner, tmp_path):
    """Exceeding ``max_rounds`` aborts and emits ``degraded="max_rounds"``."""
    _RUN_PLAN.update({"actions": 4})
    stream = io.StringIO()
    rc = runner._drive(
        {"task_intent": "t", "max_rounds": 3, "workspace_path": str(tmp_path)},
        runner.Emitter(stream),
    )
    assert rc == 1
    events = _events_from(stream)
    degrades = [
        e for e in events if isinstance(e.get("data"), dict) and e["data"].get("degraded") == "max_rounds"
    ]
    assert degrades, "a max_rounds degrade event must be emitted"
    assert degrades[0]["kind"] == "engine"
    assert degrades[0]["status"] != "ok"
    # Never a fake success on any line.
    assert not any(e.get("status") == "ok" and e.get("kind") == "result" for e in events)


def test_max_iteration_cap_value_reaches_conversation(runner, tmp_path):
    """The SDK's native round cap is set from ``max_rounds`` (defense in depth)."""
    captured_ctor: dict = {}

    class _CapturingConversation(_FakeConversation):
        def __init__(self, **kw):
            captured_ctor.update(kw)
            super().__init__(**kw)

    sdk = sys.modules["openhands.sdk"]
    original = sdk.Conversation
    sdk.Conversation = _CapturingConversation
    try:
        runner._run_conversation(
            runner.Emitter(io.StringIO()),
            {"task_intent": "t", "max_rounds": 7},
            str(tmp_path),
        )
    finally:
        sdk.Conversation = original
    assert captured_ctor.get("max_iteration_per_run") == 7


# --------------------------------------------------------------------------- #
# P0-C (3) — the wall-clock ceiling aborts a hung conversation                   #
# --------------------------------------------------------------------------- #
def test_wall_clock_abort_is_reported_verbatim(runner, tmp_path):
    """A conversation that never returns is cut off and reported ``timeout``."""
    _RUN_PLAN.update({"sleep": 3})
    stream = io.StringIO()
    rc = runner._drive(
        {
            "task_intent": "t",
            "wall_timeout_s": 1,
            "max_rounds": 30,
            "workspace_path": str(tmp_path),
        },
        runner.Emitter(stream),
    )
    assert rc == 1
    events = _events_from(stream)
    assert any(isinstance(e.get("data"), dict) and e["data"].get("degraded") == "timeout" for e in events)


# --------------------------------------------------------------------------- #
# P0-C (4) — the terminal soft-timeout knob is really set                        #
# --------------------------------------------------------------------------- #
def test_terminal_tool_sets_a_larger_no_change_timeout(runner, tmp_path):
    """The terminal tool is built with a raised soft timeout via ``Tool.params``."""
    captured = _drive_conversation(runner, tmp_path)
    terminal = next(t for t in captured["agent"]["tools"] if getattr(t, "name", None) == "terminal")
    assert terminal.params.get("no_change_timeout_seconds", 0) > 10


def test_system_prompt_warns_against_retrying_interactive_commands(runner):
    """The prompt tells the model to stop retrying interactive/stuck commands."""
    prompt = runner._SYSTEM_PROMPT
    assert "exit code -1" in prompt
    assert "interactive" in prompt.lower()


# --------------------------------------------------------------------------- #
# The engine is the single source of truth — the job must carry the knobs        #
# --------------------------------------------------------------------------- #
def test_engine_job_carries_the_llm_knobs():
    """The engine is the single source of truth — the job must carry every knob."""
    from forgeflow.codeplane.engine import CodeJob
    from forgeflow.codeplane.protocol import DEGRADED_VALUES, JOB_KEYS

    for key in ("reasoning_effort", "num_ctx", "temperature"):
        assert key in JOB_KEYS
        assert key in CodeJob().to_dict()
    assert "max_rounds" in DEGRADED_VALUES
