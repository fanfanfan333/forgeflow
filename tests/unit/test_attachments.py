"""INC2-24 — multimodal attachments into the task context (docs §2.16).

Covers the three acceptance criteria:
  * a PDF's text is injected into the task intent (reusing ``multimodal.pdf``)
  * a missing optional dependency degrades to "attachment ignored" — no raise
  * an over-limit attachment is rejected
plus the ``POST /tasks`` wiring (413 on oversize, enriched intent on success).
"""

from __future__ import annotations

import base64
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forgeflow.governance.models import EvalDecision
from forgeflow.rbac.models import UserContext
from forgeflow.runtime.attachments import (
    AttachmentInput,
    AttachmentTooLargeError,
    prepare_attachments,
    vision_available,
)
from forgeflow.runtime.orchestrator import RunHandle

pytestmark = pytest.mark.asyncio


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _fake_pypdf(pages: list[str]):
    """A stand-in ``pypdf`` module returning the given page texts."""
    mocks = [
        MagicMock(extract_text=MagicMock(return_value=text)) for text in pages
    ]
    reader = MagicMock(pages=mocks, metadata={"/Title": "Demo"})
    fake = MagicMock()
    fake.PdfReader = MagicMock(return_value=reader)
    return fake


# --------------------------------------------------------------------------- #
# PDF → intent injection                                                       #
# --------------------------------------------------------------------------- #

async def test_pdf_text_is_injected_into_intent():
    pdf = AttachmentInput(kind="pdf", name="report.pdf", data_base64=_b64(b"%PDF-1.7 fake"))
    with patch.dict(sys.modules, {"pypdf": _fake_pypdf(["page one text", "page two text"])}):
        prepared = await prepare_attachments([pdf], intent="分析这份报告")

    assert prepared.results[0].status == "injected"
    assert "page one text" in prepared.intent
    assert "page two text" in prepared.intent
    assert prepared.intent.startswith("分析这份报告")
    assert "report.pdf" in prepared.intent
    # The attachment manifest rides along in the run context.
    assert prepared.context["attachments"][0]["status"] == "injected"


# --------------------------------------------------------------------------- #
# graceful degradation when the optional extra is missing                      #
# --------------------------------------------------------------------------- #

async def test_missing_pypdf_degrades_to_ignored_without_raising():
    pdf = AttachmentInput(kind="pdf", name="scan.pdf", data_base64=_b64(b"%PDF-1.7"))
    # ``pypdf`` present-but-None makes ``import pypdf`` raise ImportError.
    with patch.dict(sys.modules, {"pypdf": None}):
        prepared = await prepare_attachments([pdf], intent="原始意图")

    assert prepared.results[0].status == "ignored"
    assert prepared.ignored == ["scan.pdf"]
    # The intent is untouched and — crucially — nothing was raised.
    assert prepared.intent == "原始意图"


async def test_unknown_attachment_kind_is_ignored():
    weird = AttachmentInput(kind="spreadsheet", name="x.xlsx", data_base64=_b64(b"data"))
    prepared = await prepare_attachments([weird], intent="hi")

    assert prepared.results[0].status == "ignored"
    assert prepared.ignored == ["x.xlsx"]


async def test_invalid_base64_is_ignored():
    bad = AttachmentInput(kind="pdf", name="bad.pdf", data_base64="!!!not-base64!!!")
    prepared = await prepare_attachments([bad], intent="hi")

    assert prepared.results[0].status == "ignored"
    assert "base64" in prepared.results[0].detail


# --------------------------------------------------------------------------- #
# size ceiling                                                                 #
# --------------------------------------------------------------------------- #

async def test_oversize_attachment_is_rejected():
    big = AttachmentInput(kind="pdf", name="big.pdf", data_base64=_b64(b"x" * 50))
    with pytest.raises(AttachmentTooLargeError, match="big.pdf"):
        await prepare_attachments([big], intent="hi", max_bytes=10)


async def test_default_limit_comes_from_settings():
    from forgeflow.config import get_settings

    limit = int(get_settings().multimodal_max_bytes)
    assert limit > 0
    just_ok = AttachmentInput(kind="raw", name="ok.bin", data_base64=_b64(b"x" * limit))
    # Within the ceiling (ignored for the unknown kind, but NOT rejected).
    prepared = await prepare_attachments([just_ok], intent="hi")
    assert prepared.results[0].status == "ignored"


# --------------------------------------------------------------------------- #
# images — metadata-only offline (no vision call)                              #
# --------------------------------------------------------------------------- #

async def test_image_is_metadata_only_when_vision_unavailable():
    png = AttachmentInput(kind="image", name="chart.png", data_base64=_b64(b"\x89PNG\r\n"))
    prepared = await prepare_attachments([png], intent="看图")

    # Offline profile has no vision model → metadata only, never an LLM call.
    assert vision_available() is False
    assert prepared.results[0].status == "metadata_only"
    assert "chart.png" in prepared.intent


# --------------------------------------------------------------------------- #
# POST /tasks wiring                                                           #
# --------------------------------------------------------------------------- #

def _allow_decision() -> EvalDecision:
    return EvalDecision(effect="allow", risk_level="low")


async def test_create_task_injects_attachment_and_runs(monkeypatch):
    from forgeflow.api.routers import tasks as tasks_router

    captured: dict[str, object] = {}

    async def _fake_run_task(task, ctx, **kwargs):
        captured["task"] = task
        captured["ctx"] = ctx
        return RunHandle(run_id="r-1", thread_id="th-1", status="completed", detail={})

    monkeypatch.setattr(tasks_router, "evaluate_task_entry", AsyncMock(return_value=_allow_decision()))
    monkeypatch.setattr(tasks_router, "run_task", _fake_run_task)

    pdf = AttachmentInput(kind="pdf", name="report.pdf", data_base64=_b64(b"%PDF-1.7"))
    request = tasks_router.TaskCreateRequestWithAttachments(
        intent="分析报告", attachments=[pdf]
    )

    with patch.dict(sys.modules, {"pypdf": _fake_pypdf(["注入的正文"])}):
        response = await tasks_router.create_task(
            request, user=UserContext(user_id="admin", role="admin"), tenant="default"
        )

    assert response.run_id == "r-1"
    assert "注入的正文" in captured["task"].intent
    assert captured["task"].context["attachments"][0]["name"] == "report.pdf"


async def test_create_task_returns_413_for_oversize_attachment(monkeypatch):
    from fastapi import HTTPException

    from forgeflow.api.routers import tasks as tasks_router

    monkeypatch.setattr(tasks_router, "evaluate_task_entry", AsyncMock(return_value=_allow_decision()))
    monkeypatch.setattr(tasks_router, "run_task", AsyncMock())

    big = AttachmentInput(kind="pdf", name="big.pdf", data_base64=_b64(b"x" * 100))
    request = tasks_router.TaskCreateRequestWithAttachments(
        intent="hi", attachments=[big]
    )

    with patch("forgeflow.runtime.attachments.get_settings") as settings_mock:
        settings_mock.return_value.multimodal_max_bytes = 10
        settings_mock.return_value.llm_provider = "mock"
        settings_mock.return_value.ollama_model = ""
        settings_mock.return_value.ollama_model_strong = ""
        with pytest.raises(HTTPException) as exc:
            await tasks_router.create_task(
                request, user=UserContext(user_id="admin", role="admin"), tenant="default"
            )

    assert exc.value.status_code == 413
