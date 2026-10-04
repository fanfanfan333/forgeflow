"""GAPFIX-INC47 (F-124 Gap A) — operator ``project_root`` injection.

Independent, additive tests for the disclosed, **user-authorized** intent-keyword
exception: when ``Settings.project_root`` is configured and the intent names the
project, ``_capability_context`` hands the planner that real path as ``repo_path``.

The load-bearing safety property is that the injection is a **bypass** — it must
NOT flip ``_is_code_task`` (which reads ``_resolve_resource_inputs``, i.e. only
``context["resources"]``). If it did, every project-referencing task would be
mis-routed into the code plane. That is asserted directly below.

These tests are deliberately separate from the T25/T34/T35/T36 suites and add no
assertion to any existing file (red line 1).
"""

from __future__ import annotations

from forgeflow.config import get_settings
from forgeflow.runtime import planning as _planning
from forgeflow.runtime.orchestrator import (
    RequestContext,
    TaskCreate,
    _capability_context,
    _is_code_task,
)

TENANT = "t-gapfix2-a"


def _ctx() -> RequestContext:
    return RequestContext(tenant_id=TENANT, user_id="u-gapfix2", role="manager")


def _task(intent: str, **context):
    return TaskCreate(intent=intent, workflow_type="generic", context=dict(context))


# --------------------------------------------------------------------------- #
# The marker table is exactly the conservative set the user authorized         #
# --------------------------------------------------------------------------- #
def test_project_reference_markers_are_the_conservative_set():
    assert _planning.PROJECT_REFERENCE_MARKERS == (
        "当前项目",
        "本项目",
        "代码库",
        "仓库",
        "项目架构",
        "this project",
        "current project",
        "repository",
        "codebase",
    )


def test_intent_references_project_positive():
    # Task A's exact ACC phrasing must hit.
    assert _planning.intent_references_project(
        "ACC_TEST_A 分析当前项目的技术架构，并总结主要模块与职责"
    )
    assert _planning.intent_references_project("analyze the current project architecture")
    assert _planning.intent_references_project("THIS PROJECT codebase overview")


def test_intent_references_project_negative_no_false_positive():
    # The data / follow-up intents from the A–E harness must NOT hit.
    assert not _planning.intent_references_project("分析这份 2025 年销售数据并生成分析报告")
    assert not _planning.intent_references_project("给出三条可执行的业务改进建议")
    assert not _planning.intent_references_project("")
    # A bare "code" word is deliberately NOT a marker (keeps false positives low).
    assert not _planning.intent_references_project("run the code and report lint findings")


# --------------------------------------------------------------------------- #
# The injection itself                                                         #
# --------------------------------------------------------------------------- #
def test_injected_when_configured_and_intent_hits(monkeypatch, tmp_path):
    root = str(tmp_path / "proj")
    monkeypatch.setattr(get_settings(), "project_root", root)
    cap = _capability_context(_task("分析当前项目的技术架构"), _ctx())
    assert cap.explicit_inputs.get("repo_path") == root


def test_not_injected_when_unset(monkeypatch):
    monkeypatch.setattr(get_settings(), "project_root", "")
    cap = _capability_context(_task("分析当前项目的技术架构"), _ctx())
    assert cap.explicit_inputs.get("repo_path") is None


def test_not_injected_when_intent_does_not_hit(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "project_root", str(tmp_path / "proj"))
    cap = _capability_context(_task("分析这份销售数据"), _ctx())
    assert cap.explicit_inputs.get("repo_path") is None


def test_real_explicit_repo_path_wins_over_project_root(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "project_root", str(tmp_path / "proj"))
    real = str(tmp_path / "real_repo")
    cap = _capability_context(_task("分析当前项目的技术架构", repo_path=real), _ctx())
    assert cap.explicit_inputs.get("repo_path") == real


def test_project_root_bypass_never_flips_is_code_task(monkeypatch, tmp_path):
    """The load-bearing safety property: the injection must NOT route to code.

    If the root flowed through ``_resolve_resource_inputs``, ``_is_code_task``
    would return True for this generic task and mis-route the whole platform.
    """
    root = str(tmp_path / "proj")
    monkeypatch.setattr(get_settings(), "project_root", root)
    task = _task("分析当前项目的技术架构")
    ctx = _ctx()
    cap = _capability_context(task, ctx)
    assert cap.explicit_inputs.get("repo_path") == root  # injected for the planner
    assert _is_code_task(task, ctx) is False             # but NOT a code task
    # And the injection never leaks back into the caller's context.
    assert task.context.get("repo_path") is None
