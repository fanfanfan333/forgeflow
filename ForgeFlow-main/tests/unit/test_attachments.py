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


# --------------------------------------------------------------------------- #
# INC16 — vision has its own slot.                                             #
#                                                                              #
# The predicate must probe exactly the slot the vision call path uses          #
# (``OLLAMA_VISION_MODEL`` via ``provider.get_vision_model``), NOT the two      #
# reasoning slots (``OLLAMA_MODEL`` / ``OLLAMA_MODEL_STRONG``). The pre-INC16   #
# code sniffed both reasoning slots while ``describe_image`` always built the    #
# *strong* slot — so a text-only strong model broke image handling while the    #
# predicate still promised vision. Routing both through one slot fixes that.    #
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """Keep ``get_settings()`` fresh so per-test env changes take effect."""
    from forgeflow.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _vision_settings(monkeypatch, **env):
    """Point ``get_settings()`` at ``env`` and return the rebuilt Settings."""
    from forgeflow.config import get_settings

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    return get_settings()


async def test_vision_unavailable_offline(monkeypatch):
    """The offline (mock) profile never advertises vision (unchanged)."""
    _vision_settings(monkeypatch, llm_provider="mock")
    assert vision_available() is False


async def test_vision_predicate_follows_the_vision_slot_not_the_reasoning_slots(monkeypatch):
    """``vision_available()`` probes ``OLLAMA_VISION_MODEL`` only — and is stable.

    Falsifiable, and the load-bearing part is the *invariance*: with a vision
    slot (``qwen2.5vl:3b``) the answer is True, and it must NOT change when the
    reasoning slots (``OLLAMA_MODEL`` / ``OLLAMA_MODEL_STRONG``) are swapped
    between a 'vl' tag and a text-only one. The old implementation varied with
    those slots — advertising an image capability the (strong-slot) call path
    could not deliver.
    """
    from forgeflow.config import get_settings

    _vision_settings(
        monkeypatch,
        llm_provider="ollama",
        ollama_vision_model="qwen2.5vl:3b",  # vision slot: capable
        ollama_model="qwen3:8b",             # reasoning slots: text-only
        ollama_model_strong="qwen3:8b",
    )
    assert vision_available() is True

    # A 'vl' reasoning pair must not change the verdict...
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5vl:3b")
    monkeypatch.setenv("OLLAMA_MODEL_STRONG", "qwen2.5vl:3b")
    get_settings.cache_clear()
    assert vision_available() is True

    # ...and a text-only reasoning pair must not either.
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:8b")
    monkeypatch.setenv("OLLAMA_MODEL_STRONG", "qwen3:8b")
    get_settings.cache_clear()
    assert vision_available() is True


async def test_vision_predicate_is_falsified_by_a_text_only_vision_slot(monkeypatch):
    """A text-only ``OLLAMA_VISION_MODEL`` honestly reports "no image capability".

    Even a 'vl' reasoning pair must not rescue it: the slot that decides is the
    one the vision call actually builds from.
    """
    _vision_settings(
        monkeypatch,
        llm_provider="ollama",
        ollama_vision_model="qwen3:8b",      # vision slot: text-only
        ollama_model="qwen2.5vl:3b",         # even a 'vl' reasoning slot…
        ollama_model_strong="qwen2.5vl:3b",  # …must not help
    )
    assert vision_available() is False


async def test_get_vision_model_is_mock_offline(monkeypatch):
    """``get_vision_model()`` must not explode (or dial out) on the offline profile."""
    from forgeflow.models import get_vision_model
    from forgeflow.models.provider import MockChatModel

    _vision_settings(monkeypatch, llm_provider="mock")
    assert isinstance(get_vision_model(), MockChatModel)


# --------------------------------------------------------------------------- #
# INC16 — the vision slot carries the SAME C1 guard as get_model(). A thinking  #
# vision model with OLLAMA_THINK=true returns EMPTY content (measured:          #
# qa_tmp/inc16/ollama_think_probe.json — 0-char content, 306-char trace,         #
# done_reason=length; think=false → 62-char JSON, done_reason=stop). Building it #
# would make image description silently return "" — a promise the path cannot    #
# keep — so it must fail fast and never masquerade as a real description.        #
# --------------------------------------------------------------------------- #

async def test_thinking_vision_slot_with_think_on_raises_and_never_fakes_a_description(
    monkeypatch,
):
    """``OLLAMA_VISION_MODEL=qwen3:8b`` + ``OLLAMA_THINK=true`` ⇒ no build, no fake.

    Two guarantees for the measured-broken combo (qa_tmp/inc16/ollama_think_probe.json):
      * ``get_vision_model()`` fails fast instead of handing back a model that
        would silently return empty content;
      * the attachment path never fabricates a description — ``describe_image`` is
        not called and the image degrades to ``metadata_only`` (never an empty /
        mock string passed off as a real vision result).
    """
    import forgeflow.multimodal.images as images_mod
    from forgeflow.models import get_vision_model

    _vision_settings(
        monkeypatch,
        llm_provider="ollama",
        ollama_vision_model="qwen3:8b",  # thinking vision slot
        ollama_think="true",             # the broken combo
    )

    # (a) the factory refuses to build it…
    with pytest.raises(ValueError, match="OLLAMA_VISION_MODEL"):
        get_vision_model()

    # (b) …and end-to-end no description is produced, the image is metadata-only.
    calls = {"n": 0}
    real_describe = images_mod.describe_image

    async def _counting_describe(*args, **kwargs):  # noqa: ANN002, ANN003
        calls["n"] += 1
        return await real_describe(*args, **kwargs)

    monkeypatch.setattr(images_mod, "describe_image", _counting_describe)

    png = AttachmentInput(kind="image", name="chart.png", data_base64=_b64(b"\x89PNG\r\n"))
    prepared = await prepare_attachments([png], intent="看图", describe_images=True)

    assert calls["n"] == 0, "a description must never be fabricated for a dead vision slot"
    assert prepared.results[0].status == "metadata_only"
    assert "chart.png" in prepared.intent


async def test_thinking_vision_slot_builds_normally_when_think_disabled(monkeypatch):
    """Same thinking vision slot with ``OLLAMA_THINK=false`` builds normally.

    Guards against a false positive: the C1 check must NOT skip a thinking model
    that is measured to work (think=false → 62-char JSON, done_reason=stop).
    """
    from forgeflow.models import get_vision_model, provider

    _vision_settings(
        monkeypatch,
        llm_provider="ollama",
        ollama_vision_model="qwen3:8b",
        ollama_think="false",
    )

    built: list[str] = []

    def _record(settings, model_name):  # noqa: ANN001
        built.append(model_name)
        return object()

    monkeypatch.setattr(provider, "_build_ollama_model", _record)

    get_vision_model()

    assert built == ["qwen3:8b"]  # built, not skipped


async def test_vision_guard_error_is_absorbed_at_the_attachment_boundary(monkeypatch):
    """A vision-hinted *thinking* tag exercises the guard through attachments.

    ``OLLAMA_VISION_MODEL=qwen3-vl:8b`` is both vision-capable (has 'vl') and a
    thinking tag (has 'qwen3'), so ``vision_available()`` is True and the vision
    path is really attempted. With ``OLLAMA_THINK=true`` the C1 guard makes
    ``get_vision_model()`` raise; ``runtime.attachments._ingest_image`` wraps the
    call in try/except and degrades to ``metadata_only`` — the error never
    surfaces as a fake/empty description. (For a text-only-named slot like
    ``qwen3:8b``, ``vision_available()`` is False and the path is not attempted at
    all; here the guard is the thing keeping the promise honest.)
    """
    import forgeflow.multimodal.images as images_mod

    _vision_settings(
        monkeypatch,
        llm_provider="ollama",
        ollama_vision_model="qwen3-vl:8b",  # vision-capable AND thinking
        ollama_think="true",
    )
    assert vision_available() is True  # the vision path really is attempted…

    calls = {"n": 0}
    real_describe = images_mod.describe_image

    async def _counting_describe(*args, **kwargs):  # noqa: ANN002, ANN003
        calls["n"] += 1
        return await real_describe(*args, **kwargs)

    monkeypatch.setattr(images_mod, "describe_image", _counting_describe)

    png = AttachmentInput(kind="image", name="chart.png", data_base64=_b64(b"\x89PNG\r\n"))
    prepared = await prepare_attachments([png], intent="看图")

    assert calls["n"] == 1, "the vision path was attempted (guard raised inside it)"
    assert prepared.results[0].status == "metadata_only", (
        "the guard error must degrade to metadata_only, never fabricate a description"
    )
