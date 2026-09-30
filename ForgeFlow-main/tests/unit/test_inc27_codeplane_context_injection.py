"""INC27 — the code plane really consumes the platform's Skill / Memory context.

Architecture (the note the user adopted): **ForgeFlow is the control plane** and
owns the Skill registry plus long-term Memory; **OpenHands is the execution
plane** and only ever receives the *selected subset* for one task. These tests
pin that seam (AC-1 / AC-2 / AC-3 / AC-4 / AC-7 / AC-15).

Two honesty rules are asserted, not merely documented:

* **nothing injected ⇒ nothing rendered**. An empty context produces ``""``, so the
  agent's prompt is byte-identical to the minimal one — never a placeholder block;
* **the injected block is appended, never substituted**. :data:`_SYSTEM_PROMPT` is
  load-bearing for the local qwen model, so it must still be the *whole* prefix
  after injection.

The runner module is loaded by path: it lives in the ``openhands`` venv's import
space (it must not be imported by ``forgeflow``), but it only needs the stdlib at
module scope, so the offline profile can exercise its renderer directly.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

import forgeflow.runtime.orchestrator as _orch
from forgeflow.codeplane.engine import CodeJob
from forgeflow.runtime.orchestrator import RequestContext, TaskCreate, _codeplane_args
from forgeflow.runtime.react_executor import ReactExecutor

_RUNNER_PATH = (
    Path(__file__).resolve().parents[2]
    / "forgeflow"
    / "codeplane"
    / "runner"
    / "run_code_task.py"
)


def _load_runner():
    spec = importlib.util.spec_from_file_location("ff_runner_inc27", _RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def runner():
    return _load_runner()


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


def test_injected_skill_and_memory_reach_the_agent_prompt(runner):
    """AC-2 — the runner really puts the injected skill/memory into the prompt."""
    block = runner._context_block(
        {"skill_context": [_SKILL], "memory_context": [_MEMORY]}
    )

    assert _SKILL["name"] in block, "the selected skill's name must reach the prompt"
    assert _SKILL["version"] in block, "the version must be recorded (AC-3)"
    assert "跑 pytest" in block, "the skill's steps must reach the prompt (AC-2)"
    assert _MEMORY["content"] in block, "the recalled memory must reach the prompt"


def test_injection_is_appended_not_substituted(runner):
    """AC-15 — the minimal prompt stays intact; injection never replaces it."""
    block = runner._context_block(
        {"skill_context": [_SKILL], "memory_context": [_MEMORY]}
    )
    full = runner._SYSTEM_PROMPT + block

    assert block, "a non-empty context must render something"
    assert full.startswith(runner._SYSTEM_PROMPT), (
        "the minimal prompt must remain the prefix — it is what keeps the local "
        "qwen model acting instead of Q&A-ing"
    )
    assert runner._SYSTEM_PROMPT in full


def test_nothing_injected_renders_nothing(runner):
    """AC-4 / AC-7 — no context ⇒ no block, and the prompt is byte-identical."""
    assert runner._context_block({}) == ""
    assert runner._context_block({"skill_context": [], "memory_context": []}) == ""
    assert runner._context_block({"skill_context": [], "memory_context": [_MEMORY]}) != ""

    # Honest absence: with nothing injected the prompt is exactly the minimal one.
    assert runner._SYSTEM_PROMPT + runner._context_block({}) == runner._SYSTEM_PROMPT


def test_malformed_context_is_ignored_not_fabricated(runner):
    """A junk payload must degrade to 'nothing', never to invented context."""
    assert runner._context_block({"skill_context": "not-a-list"}) == ""
    assert runner._context_block({"memory_context": {"a": 1}}) == ""
    assert runner._context_block({"skill_context": [None, 7, {"name": ""}]}) == ""


def test_codeplane_args_carry_the_selected_context():
    """AC-1 — the selected context travels into the code tool's args."""
    ctx = RequestContext(
        tenant_id="t-1",
        injected_skills=[_SKILL],
        injected_memory=[_MEMORY],
    )
    args = _codeplane_args(TaskCreate(intent="修复登录 500"), ctx)

    assert args["skill_context"] == [_SKILL]
    assert args["memory_context"] == [_MEMORY]


def test_codeplane_args_omit_empty_context():
    """AC-4 / AC-7 — empty ⇒ the key is absent, not an empty shell."""
    ctx = RequestContext(tenant_id="t-1")
    args = _codeplane_args(TaskCreate(intent="修复登录 500"), ctx)

    assert "skill_context" not in args
    assert "memory_context" not in args
    # No run context at all (every pre-INC27 call site) must still work.
    assert _codeplane_args(TaskCreate(intent="修复登录 500")) == {}


def test_job_dict_carries_context_to_the_runner_stdin():
    """AC-1 — the fields survive ``to_dict`` (what is written to the runner)."""
    payload = CodeJob(
        run_id="run-1",
        task_intent="修复登录 500",
        workspace_path="/tmp/ws",
        skill_context=[_SKILL],
        memory_context=[_MEMORY],
    ).to_dict()

    assert payload["skill_context"] == [_SKILL]
    assert payload["memory_context"] == [_MEMORY]

    # Default-safe: a job built without context (every existing site) is empty.
    bare = CodeJob(run_id="run-2", task_intent="x", workspace_path="/tmp/ws").to_dict()
    assert bare["skill_context"] == []
    assert bare["memory_context"] == []


# --------------------------------------------------------------------------- #
# INC28 W5 — the real-provider (react) path must ALSO carry the code-plane args  #
# --------------------------------------------------------------------------- #
# Before W5 the react executor only merged ``_capability_context`` inputs, never
# ``orchestrator._codeplane_args``, so under a real provider ``code.execute`` lost
# the selected skill/memory context (§8/§9 injection silently empty) and an
# approval resume could not find its ``workspace_id``. These assertions are
# mechanical; the counterfactual proves the merge is load-bearing.


def _code_task(context: dict | None = None) -> TaskCreate:
    return TaskCreate(intent="修复登录 500", context=context or {})


def test_react_code_execute_carries_the_selected_context():
    """Real provider (react) ⇒ ``code.execute`` args carry the injected context."""
    ctx = RequestContext(
        tenant_id="t-1", injected_skills=[_SKILL], injected_memory=[_MEMORY]
    )
    args, injected = ReactExecutor._effective_args(
        "code.execute", {}, _code_task(), ctx, []
    )

    assert args["skill_context"] == [_SKILL]
    assert args["memory_context"] == [_MEMORY]
    assert "skill_context" in injected and "memory_context" in injected


def test_react_resume_workspace_id_reaches_code_execute():
    """An approval resume must hand ``code.execute`` the platform's workspace."""
    task = _code_task(
        {"codeplane_approval": "approved", "codeplane_workspace_id": "ws-123"}
    )
    args, _ = ReactExecutor._effective_args(
        "code.execute", {}, task, RequestContext(tenant_id="t-1"), []
    )

    assert args["workspace_id"] == "ws-123"
    assert args["approval"] == "approved"


def test_react_never_trusts_a_model_supplied_codeplane_authority():
    """The model may not smuggle in ``approval`` / ``workspace_id`` / ``prior``."""
    args, _ = ReactExecutor._effective_args(
        "code.execute",
        {"approval": "approved", "workspace_id": "ws-evil", "prior": {"diff": "x"}},
        _code_task(),
        RequestContext(tenant_id="t-1"),
        [],
    )

    assert "approval" not in args, "模型自造的 approval 不得进入执行参数"
    assert "workspace_id" not in args, "模型自造的 workspace_id 不得进入执行参数"
    assert "prior" not in args, "模型自造的 prior 不得进入执行参数"


def test_react_does_not_inject_codeplane_context_into_non_code_tools(monkeypatch):
    """The merge is scoped to the code tools — a normal tool is unaffected."""
    ctx = RequestContext(tenant_id="t-1", injected_skills=[_SKILL])
    args, _ = ReactExecutor._effective_args(
        "research.search", {}, _code_task(), ctx, []
    )
    assert "skill_context" not in args


def test_counterfactual_without_the_merge_the_context_is_lost(monkeypatch):
    """Falsifiable: revert the code-plane merge ⇒ the context disappears (RED)."""
    ctx = RequestContext(
        tenant_id="t-1", injected_skills=[_SKILL], injected_memory=[_MEMORY]
    )

    # Positive control — with the fix the context reaches the args.
    args, _ = ReactExecutor._effective_args(
        "code.execute", {}, _code_task(), ctx, []
    )
    assert args["skill_context"] == [_SKILL]

    # Revert the seam: the react executor reads ``_codeplane_args`` at call time,
    # so neutralising it must make the context vanish.
    monkeypatch.setattr(_orch, "_codeplane_args", lambda task, ctx=None: {})
    reverted, _ = ReactExecutor._effective_args(
        "code.execute", {}, _code_task(), ctx, []
    )
    assert "skill_context" not in reverted
    assert "memory_context" not in reverted
