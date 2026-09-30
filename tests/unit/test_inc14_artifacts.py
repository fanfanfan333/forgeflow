"""INC14 — the run's deliverables projection (任务产物) unit tests.

``forgeflow.runtime.artifacts.artifacts_from_invocations`` is the **single**
projection point between a run's honest evidence trail
(``task.context["tool_invocations"]`` → ``RunRecord.tool_invocations``) and the
``artifacts`` the task-result page renders. Its whole contract is honesty:

  * a successful (``status == "ok"``) ``report.render`` that returned a non-empty
    ``payload["content"]`` becomes **exactly one** artifact;
  * every other shape (non-``ok`` status, a non-dict payload, a missing / empty /
    whitespace-only ``content``) yields **no** artifact — the UI then shows its
    honest empty state instead of a fabricated result;
  * the artifact's ``content`` is the payload's ``content`` **verbatim** (never
    concatenated / translated / templated) and ``result_ref`` is passed through
    unchanged, so the artifact ⇄ evidence join survives.

Every case declares its storage tier explicitly via ``force_memory_backend``
(the projection itself is pure, but the tier must never be inherited from the
environment — see ``tests/conftest.py``).
"""

from __future__ import annotations

import hashlib

from forgeflow.runtime.artifacts import (
    ARTIFACT_KIND_MARKDOWN,
    ARTIFACT_TOOL,
    artifacts_from_invocations,
)

#: A real report body, as ``tool_handlers.report_render`` emits it.
_REPORT_CONTENT = "# 运行报告\n\n**意图**：为 Acme 整理销售线索分析摘要\n\n共 3 步。"
_REPORT_REF = hashlib.sha256(_REPORT_CONTENT.encode("utf-8")).hexdigest()[:32]


def _report_render_invocation(
    *,
    content: str | None = _REPORT_CONTENT,
    status: str = "ok",
    result_ref: str | None = _REPORT_REF,
    payload: object | None = None,
    started_at: str = "2026-01-02T03:04:05+00:00",
    tool: str = "report.render",
) -> dict:
    """Build a ``ToolInvocation.to_dict()``-shaped dict for the report step."""
    if payload is None:
        payload = {
            "ok": True,
            "provider": "stdlib-render",
            "content": content,
            "result_ref": result_ref,
            "observation_count": 3,
            "summary": "渲染 3 条 observation 为 Markdown",
        }
    return {
        "tool": tool,
        "status": status,
        "result_ref": result_ref,
        "started_at": started_at,
        "payload": payload,
    }


# --------------------------------------------------------------------------- #
# 1. A successful report.render → exactly one artifact                          #
# --------------------------------------------------------------------------- #
def test_successful_report_render_projects_one_artifact(force_memory_backend):
    invocations = [
        {"tool": "research.search", "status": "ok", "payload": {"ok": True}},
        _report_render_invocation(),
    ]

    artifacts = artifacts_from_invocations("run-1", invocations)

    assert len(artifacts) == 1
    art = artifacts[0]
    assert art["kind"] == ARTIFACT_KIND_MARKDOWN
    assert art["format"] == "markdown"
    assert art["title"] == "运行报告"
    assert art["source"] == ARTIFACT_TOOL
    assert art["id"] == f"run-1:artifact:{_REPORT_REF}"
    assert art["created_at"] == "2026-01-02T03:04:05+00:00"


# --------------------------------------------------------------------------- #
# 2. content is verbatim from payload["content"] (never reformatted)            #
# --------------------------------------------------------------------------- #
def test_content_is_verbatim_from_the_payload(force_memory_backend):
    content = "# 运行报告\n\n- 逐字\n- 保真\n最后一行。"
    invocations = [_report_render_invocation(content=content)]

    artifacts = artifacts_from_invocations("run-verbatim", invocations)

    assert len(artifacts) == 1
    assert artifacts[0]["content"] == content
    assert artifacts[0]["content"] is not None


# --------------------------------------------------------------------------- #
# 3. result_ref is passed through unchanged (artifact ⇄ evidence join)          #
# --------------------------------------------------------------------------- #
def test_result_ref_is_passed_through(force_memory_backend):
    invocations = [_report_render_invocation(result_ref="deadbeef" * 4)]

    artifacts = artifacts_from_invocations("run-ref", invocations)

    assert len(artifacts) == 1
    assert artifacts[0]["result_ref"] == "deadbeef" * 4


# --------------------------------------------------------------------------- #
# 4. Honest empty state — every non-qualifying shape yields NO artifact         #
# --------------------------------------------------------------------------- #
def test_non_ok_status_projects_no_artifact(force_memory_backend):
    for status in ("error", "unavailable", "refused", "skipped"):
        invocations = [_report_render_invocation(status=status)]
        assert artifacts_from_invocations("run-x", invocations) == [], status


def test_non_dict_payload_projects_no_artifact(force_memory_backend):
    invocations = [_report_render_invocation(payload="not-a-dict")]
    assert artifacts_from_invocations("run-x", invocations) == []


def test_missing_content_projects_no_artifact(force_memory_backend):
    invocations = [_report_render_invocation(payload={"ok": True})]
    assert artifacts_from_invocations("run-x", invocations) == []


def test_empty_or_whitespace_content_projects_no_artifact(force_memory_backend):
    for content in ("", "   ", "\n\t ", "\r\n"):
        invocations = [_report_render_invocation(content=content)]
        assert artifacts_from_invocations("run-x", invocations) == [], repr(content)


def test_other_tools_are_never_projected(force_memory_backend):
    """A non-report tool that happens to carry ``content`` is not a deliverable."""
    invocations = [
        {
            "tool": "docs.parse",
            "status": "ok",
            "result_ref": "abc",
            "started_at": "2026-01-02T03:04:05+00:00",
            "payload": {"ok": True, "content": "some parsed text"},
        }
    ]
    assert artifacts_from_invocations("run-x", invocations) == []


# --------------------------------------------------------------------------- #
# 5. Robustness — malformed input degrades to [] (never raises)                 #
# --------------------------------------------------------------------------- #
def test_malformed_invocations_are_skipped_without_raising(force_memory_backend):
    invocations = [
        None,
        "not-a-dict",
        {"tool": "report.render"},  # no status/payload
        42,
        _report_render_invocation(),
    ]

    artifacts = artifacts_from_invocations("run-robust", invocations)

    assert len(artifacts) == 1
    assert artifacts[0]["source"] == ARTIFACT_TOOL


def test_empty_trail_and_none_yield_no_artifact(force_memory_backend):
    assert artifacts_from_invocations("run-empty", []) == []
    assert artifacts_from_invocations("run-none", None) == []


# --------------------------------------------------------------------------- #
# 6. created_at falls back to the run's completion time when absent             #
# --------------------------------------------------------------------------- #
def test_created_at_falls_back_to_the_supplied_timestamp(force_memory_backend):
    inv = _report_render_invocation()
    inv["started_at"] = ""  # no started_at on the invocation

    artifacts = artifacts_from_invocations(
        "run-fallback", [inv], fallback_created_at="2026-09-09T09:09:09+00:00"
    )

    assert len(artifacts) == 1
    assert artifacts[0]["created_at"] == "2026-09-09T09:09:09+00:00"
