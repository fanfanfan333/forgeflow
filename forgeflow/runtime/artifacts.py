"""INC14 — the run's real deliverables (任务产物).

Single projection point: a run's artifact is derived from its own stored
``tool_invocations``. Three producers today: a successful ``report.render``
step (the run report), a successful ``code.commit`` (the approved code diff /
test verdict), and a successful ``document.edit`` / ``artifact.save`` (INC43 S4
— the edited DOCX). The projection never fabricates content: an absent /
truncated payload yields no artifact.

The projection is a **pure function** of the run's own evidence trail
(``ToolInvocation.to_dict()`` dicts, see :mod:`forgeflow.runtime.tool_executor`):
it reads only ``payload["content"]`` verbatim (or, for a DOCX, the small
``artifact_ref`` summary) — it never concatenates, translates or templates
anything. When no producer ran (or its payload was bounded away) there is simply
**no** artifact, so the caller degrades to an honest empty state instead of
inventing a result.

Known boundary — payload truncation: the executor bounds every stored
``payload`` at ``MAX_PAYLOAD_CHARS`` (4000, see
``forgeflow.runtime.tool_executor._bound_payload``). A real ``report.render``
result whose JSON form exceeds that ceiling is replaced by a
``{"truncated": True, ...}`` envelope that carries **no** ``content`` key, so
this projection yields no artifact (an honest empty state) rather than rendering
a half-cut body. Today the offline 4-step report is ~400-600 characters, well
inside the ceiling.

INC43 S4 — that ceiling is exactly why a DOCX deliverable is **not** carried as
base64 ``content``: a real DOCX is tens of KB, so a base64 body would be
truncated away and the artifact could never be projected. Instead the bytes live
in ``documents.store.DocArtifactStore`` (outside the project tree) and the
artifact carries ``content_ref`` — the read side
(``api.routers.runs.download_artifact``) fetches the bytes from there.

NOTE: the hub run store is process-lifetime; hub runs are not persisted, so a
restart drops every artifact exactly as it drops the run itself.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ARTIFACT_KIND_MARKDOWN",
    "ARTIFACT_KIND_CODE_DIFF",
    "ARTIFACT_KIND_CODE_TEST",
    "ARTIFACT_KIND_DOCX",
    "ARTIFACT_TOOL",
    "ARTIFACT_CODE_TOOL",
    "ARTIFACT_DOC_TOOL",
    "ARTIFACT_SAVE_TOOL",
    "artifacts_from_invocations",
]

#: The one plan tool whose successful invocation becomes a deliverable.
ARTIFACT_TOOL = "report.render"
#: The artifact ``kind`` every ``report.render`` deliverable carries.
ARTIFACT_KIND_MARKDOWN = "report_markdown"
#: INC25 W2 — the code-execution plane's deliverables. A successful
#: ``code.commit`` (i.e. an **approved** change) yields the diff and — when the
#: test evidence was reviewed — the test verdict. Nothing is produced while the
#: change is merely ``awaiting_approval`` (a pending change is not a deliverable).
ARTIFACT_CODE_TOOL = "code.commit"
ARTIFACT_KIND_CODE_DIFF = "code_diff"
ARTIFACT_KIND_CODE_TEST = "code_test_report"
#: INC43 S4 — the DOCX document-editing plane. A successful ``document.edit``
#: (the Tool layer that really wrote the bytes) yields the DOCX deliverable; a
#: successful ``artifact.save`` registers the very same deliverable. The bytes
#: live OUTSIDE the run payload (``documents.store.DocArtifactStore``) and the
#: artifact carries only the content-addressed ``content_ref`` — a base64 blob
#: would blow past ``MAX_PAYLOAD_CHARS`` (4000) and vanish into a ``truncated``
#: envelope, so the deliverable could never be projected (see the module note).
ARTIFACT_DOC_TOOL = "document.edit"
ARTIFACT_SAVE_TOOL = "artifact.save"
ARTIFACT_KIND_DOCX = "document_docx"


def _render_test_summary(tests: dict[str, Any]) -> str:
    """Render a :class:`~forgeflow.codeplane.tests_verdict.TestResult` dict honestly.

    An unmeasured result is stated as *未测量* — it is **never** dressed up as a
    pass (AC-16): "no parseable evidence" must not read as "green".
    """
    if tests.get("measured") is not True:
        return "测试结果无法解析，记为未测量（不判通过）。"
    return (
        f"命令：{tests.get('command') or '（未记录）'}\n"
        f"通过 {int(tests.get('passed') or 0)} / "
        f"未通过 {int(tests.get('failed') or 0)} / "
        f"错误 {int(tests.get('errors') or 0)}\n"
        f"结论：{tests.get('verdict') or 'unmeasured'}"
    )


def _code_artifacts(
    run_id: str, inv: dict[str, Any], payload: dict[str, Any], created_at: str, idx: int
) -> list[dict[str, Any]]:
    """Project an approved ``code.commit`` invocation into diff / test deliverables."""
    out: list[dict[str, Any]] = []
    ref = str(inv.get("result_ref") or "")
    suffix = ref or str(idx)
    diff = payload.get("diff")
    if isinstance(diff, str) and diff.strip():
        out.append({
            "id": f"{run_id}:artifact:{suffix}:code_diff",
            "kind": ARTIFACT_KIND_CODE_DIFF,
            "title": "代码变更（Diff）",
            "format": "diff",
            "content": diff,
            "source": ARTIFACT_CODE_TOOL,
            "result_ref": ref,
            "created_at": created_at,
        })
    tests = payload.get("test_result")
    if isinstance(tests, dict) and tests:
        out.append({
            "id": f"{run_id}:artifact:{suffix}:code_test",
            "kind": ARTIFACT_KIND_CODE_TEST,
            "title": "测试结论",
            "format": "text",
            "content": _render_test_summary(tests),
            "source": ARTIFACT_CODE_TOOL,
            "result_ref": ref,
            "created_at": created_at,
        })
    return out


def _docx_artifact(
    run_id: str, inv: dict[str, Any], payload: dict[str, Any], created_at: str, idx: int
) -> dict[str, Any] | None:
    """Project a successful DOCX-producing invocation into one artifact.

    Returns ``None`` when the payload carries no usable ``artifact_ref`` — an
    absent reference yields **no** artifact (an honest empty state) rather than a
    fabricated one. Only the small summary is read; the bytes stay on disk and
    are fetched by ``api.routers.runs.download_artifact`` via ``content_ref``.
    """
    artifact_ref = payload.get("artifact_ref")
    if not isinstance(artifact_ref, str) or not artifact_ref.strip():
        return None
    ref = str(inv.get("result_ref") or "")
    filename = str(payload.get("filename") or "").strip() or "document.docx"
    diff = {
        "modified": int(payload.get("modified") or 0),
        "added": int(payload.get("added") or 0),
        "removed": int(payload.get("removed") or 0),
        "numeric_changes": int(payload.get("numeric_changes") or 0),
    }
    return {
        "id": f"{run_id}:artifact:{ref or idx}:docx",
        "kind": ARTIFACT_KIND_DOCX,
        "title": filename,
        "format": "docx",
        # ``content`` is deliberately empty: the real bytes live behind
        # ``content_ref`` (a base64 body would be truncated away by the payload
        # ceiling and the artifact would never appear).
        "content": "",
        "content_ref": artifact_ref.strip(),
        "source": ARTIFACT_DOC_TOOL,
        "result_ref": ref,
        "created_at": created_at,
        "diff": diff,
    }


def artifacts_from_invocations(
    run_id: str,
    invocations: list[dict[str, Any]] | None,
    *,
    fallback_created_at: str = "",
) -> list[dict[str, Any]]:
    """Project a run's successful deliverable invocations into artifacts.

    Two producers today:

      * ``report.render`` (INC14) → one ``report_markdown`` artifact (unchanged);
      * ``code.commit`` (INC25 W2) → a ``code_diff`` artifact (the approved change)
        and a ``code_test_report`` artifact (the reviewed test verdict).

    Args:
        run_id: The owning run's id — used to build each artifact's stable
            ``id`` (``{run_id}:artifact:{result_ref|index}``).
        invocations: The run's cumulative tool-invocation trail (each a
            ``ToolInvocation.to_dict()``). Malformed, non-``ok`` and non-``dict``
            entries are skipped rather than raising.
        fallback_created_at: Timestamp used only when an invocation carries no
            ``started_at`` (typically the run's own completion time).

    Returns:
        A list of artifact dicts, one per qualifying invocation. Empty when the
        run produced no such evidence — never a fabricated placeholder.
    """
    out: list[dict[str, Any]] = []
    seen_docx_refs: set[str] = set()
    for idx, inv in enumerate(invocations or []):
        if not isinstance(inv, dict):
            continue
        if inv.get("status") != "ok":
            continue
        payload = inv.get("payload")
        if not isinstance(payload, dict):
            continue
        tool = inv.get("tool")
        created_at = str(inv.get("started_at") or fallback_created_at or "")
        if tool == ARTIFACT_TOOL:
            content = payload.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            ref = str(inv.get("result_ref") or "")
            out.append({
                "id": f"{run_id}:artifact:{ref or idx}",
                "kind": ARTIFACT_KIND_MARKDOWN,
                "title": "运行报告",
                "format": "markdown",
                "content": content,
                "source": ARTIFACT_TOOL,
                "result_ref": ref,
                "created_at": created_at,
            })
        elif tool == ARTIFACT_CODE_TOOL:
            out.extend(_code_artifacts(run_id, inv, payload, created_at, idx))
        elif tool in (ARTIFACT_DOC_TOOL, ARTIFACT_SAVE_TOOL):
            # Both the writer (``document.edit``) and the registrar
            # (``artifact.save``) project the same deliverable. They are
            # de-duplicated by ``content_ref`` so a run that ran both still
            # yields exactly ONE DOCX artifact.
            artifact = _docx_artifact(run_id, inv, payload, created_at, idx)
            if artifact is None:
                continue
            content_ref = str(artifact.get("content_ref") or "")
            if content_ref and content_ref in seen_docx_refs:
                continue
            if content_ref:
                seen_docx_refs.add(content_ref)
            out.append(artifact)
    return out
