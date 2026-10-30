"""Typed, by-reference cross-project handoff payload.

Upgrade following the surveyed frameworks (LangGraph, OpenAI Agents
SDK, Google ADK, A2A, OpenHands, Claude Agent SDK): none of them
provides cross-run, cross-tenant handoff by ``source_run_id`` out of
the box — that is the application layer's job. This module is that
layer for ForgeFlow, on top of ``result_handoff``'s inline items:

* ``HandoffPayload`` is a Pydantic model validated server-side from
  the stored source run, never from model-generated arguments
  (OpenAI ``input_type`` pattern, but the input here is the run
  record, not the model's tool call).
* Documents travel **by reference** (ADK ``ArtifactService`` pattern):
  ``artifact_id`` + ``content_ref`` + ``sha256`` + ``format``. The
  inline text in ``handoff_results`` is the codeplane tool's working
  copy; the reference is the audit identity, and a hash mismatch
  between the two is a rejection, not a silent downgrade.
* ``status`` is explicit (ADK ``partial``/``errorCode`` pattern):
  only ``completed`` may flow to a commit gate. ``partial`` may be
  inspected, with missing documents listed; ``failed`` carries only
  its error record, no documents, no diff, no final answer presented
  as fact.
* The diff is **server-computed** from the stored run's codeplane
  diff (OpenHands ``git diff`` review pattern): the payload carries
  its sha256, and the approval gate must compare against that hash,
  never against an agent-claimed diff.
* Provenance and audit are fields, not prose: source run/thread,
  tenant, target project, produced time, payload hash, and a
  ``missing`` list. A consumer that ignores ``status`` or the hash
  is misusing the payload, not reading a different one.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from forgeflow.runtime.result_handoff import _artifact_text, _text


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _raw_bytes(art: dict[str, Any]) -> bytes | None:
    ref = _text(art.get("content_ref"))
    if not ref:
        return None
    try:
        from forgeflow.documents.store import DocArtifactStore

        return DocArtifactStore().get(ref)
    except Exception:  # noqa: BLE001 — one bad artifact must not kill the handoff
        return None


class DocumentRef(BaseModel):
    artifact_id: str
    title: str
    format: str = ""
    content_ref: str = ""
    sha256: str
    truncated: bool = False
    missing: bool = False


class DiffRef(BaseModel):
    sha256: str
    truncated: bool = False


class HandoffPayload(BaseModel):
    source_run_id: str
    source_thread_id: str = ""
    tenant_id: str = ""
    target_project_id: str = ""
    derived_from: Literal["source_run", "explicit"] = "source_run"
    status: Literal["completed", "partial", "failed"]
    documents: list[DocumentRef] = Field(default_factory=list)
    final_answer: str = ""
    diff: DiffRef | None = None
    errors: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    produced_at: str = ""
    payload_hash: str = ""

    def audit_record(self, *, target_run_id: str = "") -> dict[str, Any]:
        """One structured audit record per handoff (A2A audit pattern)."""
        return {
            "source_run_id": self.source_run_id,
            "source_thread_id": self.source_thread_id,
            "target_run_id": target_run_id,
            "target_project_id": self.target_project_id,
            "tenant_id": self.tenant_id,
            "status": self.status,
            "payload_hash": self.payload_hash,
            "document_count": len(self.documents),
            "diff_sha256": self.diff.sha256 if self.diff else None,
            "missing": list(self.missing),
            "produced_at": self.produced_at,
        }


def _payload_hash(body: dict[str, Any]) -> str:
    return _sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8"))


def build_handoff_payload(
    record: Any,
    *,
    tenant_id: str | None = None,
    user_id: str | None = None,
    target_project_id: str = "",
) -> HandoffPayload:
    """Build a validated payload from one stored source run.

    Fail-closed on tenant/user mismatch, exactly like
    ``build_handoff_results``. ``status`` is derived from the run's
    own ``status``/``outcome``: ``completed`` + success + no missing
    documents ⇒ completed; a run that recorded usable outputs but
    did not complete cleanly, or whose documents are partly missing,
    ⇒ partial; a run with no usable outputs ⇒ failed, with its
    error list carried and no documents/diff/answer presented, and
    the withheld document titles named in ``missing``.
    """
    if record is None:
        raise ValueError("跨项目结果来源运行不存在或无权访问")
    if tenant_id is not None and getattr(record, "tenant_id", None) not in (None, tenant_id):
        raise ValueError("跨项目结果来源运行不存在或无权访问")
    if user_id is not None:
        actor = getattr(record, "actor_user_id", None)
        if actor not in (None, "", user_id):
            raise ValueError("跨项目结果来源运行不存在或无权访问")

    run_id = _text(getattr(record, "run_id", "")) or "unknown-run"
    run_status = _text(getattr(record, "status", ""))
    outcome = _text(getattr(record, "outcome", ""))
    errors = [str(e) for e in (getattr(record, "errors", None) or []) if str(e or "").strip()]

    documents: list[DocumentRef] = []
    missing: list[str] = []
    artifacts = [a for a in (getattr(record, "artifacts", None) or []) if isinstance(a, dict)]
    for art in artifacts[:8]:
        title = _text(art.get("title") or art.get("kind") or "artifact")
        text = _artifact_text(art)
        raw = _raw_bytes(art)
        if raw is not None:
            digest = _sha256(raw)
        elif text:
            digest = _sha256(text.encode("utf-8"))
        else:
            missing.append(title)
            documents.append(
                DocumentRef(
                    artifact_id=_text(art.get("id") or art.get("result_ref") or title),
                    title=title,
                    format=_text(art.get("format")).lower(),
                    content_ref=_text(art.get("content_ref")),
                    sha256="",
                    missing=True,
                )
            )
            continue
        documents.append(
            DocumentRef(
                artifact_id=_text(art.get("id") or art.get("result_ref") or title),
                title=title,
                format=_text(art.get("format")).lower(),
                content_ref=_text(art.get("content_ref")),
                sha256=digest,
                truncated=len(text.encode("utf-8")) > 12000,
            )
        )

    llm = getattr(record, "llm", None) or {}
    answer = llm.get("final_answer") or {} if isinstance(llm, dict) else {}
    answer_text = _text(answer.get("text")) if isinstance(answer, dict) else ""

    codeplane = getattr(record, "codeplane", None) or {}
    diff_text = _text(codeplane.get("diff")) if isinstance(codeplane, dict) else ""
    diff = (
        DiffRef(
            sha256=_sha256(diff_text.encode("utf-8")),
            truncated=len(diff_text.encode("utf-8")) > 12000,
        )
        if diff_text
        else None
    )

    usable = bool([d for d in documents if not d.missing] or answer_text or diff_text)
    if run_status == "completed" and outcome in ("success", "succeeded", "completed") and not missing:
        status: Literal["completed", "partial", "failed"] = "completed"
    elif usable:
        status = "partial"
    else:
        status = "failed"

    if status == "failed":
        # Compute the withheld titles BEFORE clearing: the old order
        # cleared documents first, so the missing list never named the
        # documents that were withheld and a failed payload looked
        # empty without explanation.
        missing = [d.title for d in documents if not d.missing] + missing
        documents = []
        answer_text = ""
        diff = None

    body: dict[str, Any] = {
        "source_run_id": run_id,
        "source_thread_id": _text(getattr(record, "thread_id", "")),
        "tenant_id": tenant_id or _text(getattr(record, "tenant_id", "")),
        "target_project_id": target_project_id,
        "derived_from": "source_run",
        "status": status,
        "documents": [d.model_dump() for d in documents],
        "final_answer": answer_text[:12000],
        "diff": diff.model_dump() if diff else None,
        "errors": errors,
        "missing": missing,
        "produced_at": _text(
            getattr(record, "completed_at", "") or getattr(record, "created_at", "")
        )
        or datetime.now(UTC).isoformat(),
    }
    body["payload_hash"] = _payload_hash(body)
    return HandoffPayload.model_validate(body)


def build_explicit_payload(
    items: list[dict[str, Any]],
    *,
    tenant_id: str | None = None,
    target_project_id: str = "",
) -> HandoffPayload:
    """Typed manifest for a caller-supplied ``handoff_results`` list.

    Explicit items have no source run to audit against, so they are
    marked ``derived_from="explicit"`` and ``status="partial"`` —
    never ``completed`` — and their document hashes are computed from
    the supplied content itself. This closes the gap where the
    explicit path bypassed the payload entirely and left no manifest
    in the run record.
    """
    documents: list[DocumentRef] = []
    final_answer = ""
    for item in items:
        if not isinstance(item, dict):
            continue
        content = _text(item.get("content"))
        if not content:
            continue
        if item.get("kind") == "research" and not final_answer:
            final_answer = content[:12000]
        elif item.get("kind") == "document":
            documents.append(
                DocumentRef(
                    artifact_id=_text(item.get("source") or "explicit"),
                    title=_text(item.get("source") or "explicit"),
                    sha256=_sha256(content.encode("utf-8")),
                    truncated=len(content.encode("utf-8")) > 12000,
                )
            )
    body: dict[str, Any] = {
        "source_run_id": "",
        "source_thread_id": "",
        "tenant_id": tenant_id or "",
        "target_project_id": target_project_id,
        "derived_from": "explicit",
        "status": "partial",
        "documents": [d.model_dump() for d in documents],
        "final_answer": final_answer,
        "diff": None,
        "errors": [],
        "missing": [],
        "produced_at": datetime.now(UTC).isoformat(),
    }
    body["payload_hash"] = _payload_hash(body)
    return HandoffPayload.model_validate(body)
