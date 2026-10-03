"""INC45 T01 — PDF-plane layered pins (offline; no model, no PG).

Nails the requirements of the ``pdf.*`` domain (design §1.2 / §8):

  1. **Reuse of the one truth** — ``inspect_pdf`` delegates to
     :func:`forgeflow.multimodal.pdf.extract_pdf_text` (``page_count`` matches).
  2. **Layering** — ``resolve_spec`` (LLM) never writes; ``apply_spec`` (Tool) is
     the only writer, and the PDF it emits is re-parseable (non-empty pages).
  3. **Honest degradation** — a missing ``pypdf`` ⇒ ``PdfInspectionError``, a
     missing ``fpdf2`` ⇒ ``PdfGenerationError`` (both verbatim; no empty PDF).
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from forgeflow.documents import (
    PdfGenerateSpec,
    PdfGenerationError,
    PdfInspectionError,
    apply_pdf_spec,
    inspect_pdf,
    resolve_pdf_spec,
    verify_pdf,
)

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #
def _pdf_bytes(*, title: str = "标题", paragraph: str = "正文内容一") -> bytes:
    """A small real PDF, produced by the writer under test (fpdf2)."""
    pytest.importorskip("fpdf")
    return apply_pdf_spec(PdfGenerateSpec(title=title, paragraphs=[paragraph]))


class _FakeModel:
    def __init__(self, content: str) -> None:
        self._content = content
        self.calls = 0

    async def ainvoke(self, _messages):
        self.calls += 1
        return SimpleNamespace(content=self._content)


@pytest.fixture
def no_model(monkeypatch):
    import forgeflow.models.provider as provider

    monkeypatch.setattr(provider, "get_model", lambda *a, **k: None)


@pytest.fixture
def hide_pypdf(monkeypatch):
    monkeypatch.setitem(sys.modules, "pypdf", None)


@pytest.fixture
def hide_fpdf(monkeypatch):
    monkeypatch.setitem(sys.modules, "fpdf", None)


# --------------------------------------------------------------------------- #
# 1. inspect reuses the single truth                                           #
# --------------------------------------------------------------------------- #
def test_inspect_pdf_page_count_matches_extract_pdf_text():
    from forgeflow.multimodal.pdf import extract_pdf_text

    data = _pdf_bytes()
    facts = inspect_pdf(data)
    assert facts.page_count == extract_pdf_text(data).page_count
    assert facts.page_count == 1
    assert facts.chars is not None and facts.chars > 0
    assert len(facts.page_chars) == 1


def test_inspect_pdf_is_tri_state_on_unreadable_bytes():
    with pytest.raises(PdfInspectionError):
        inspect_pdf(b"not a pdf")
    with pytest.raises(PdfInspectionError):
        inspect_pdf(b"")


# --------------------------------------------------------------------------- #
# 2. Layering                                                                  #
# --------------------------------------------------------------------------- #
async def test_resolve_spec_returns_none_without_model(no_model):
    assert await resolve_pdf_spec("生成一份合同 PDF") is None


async def test_resolve_spec_parses_without_writing(monkeypatch):
    model = _FakeModel('{"title":"报告","paragraphs":["第一段"],"page_size":"A4"}')
    import forgeflow.models.provider as provider

    monkeypatch.setattr(provider, "get_model", lambda *a, **k: model)
    spec = await resolve_pdf_spec("生成一份报告")
    assert isinstance(spec, PdfGenerateSpec)
    assert spec.title == "报告" and spec.paragraphs == ["第一段"]
    assert model.calls == 1


def test_apply_spec_emits_a_reparseable_non_empty_pdf():
    from forgeflow.multimodal.pdf import extract_pdf_text

    spec = PdfGenerateSpec(title="季度报告", paragraphs=["第一段内容", "第二段内容"])
    data = apply_pdf_spec(spec)
    assert len(data) > 0
    document = extract_pdf_text(data)
    assert document.page_count >= 1
    assert len(document.text) > 0


def test_apply_spec_rejects_unknown_page_size():
    with pytest.raises(PdfGenerationError):
        apply_pdf_spec(PdfGenerateSpec(title="x", paragraphs=["y"], page_size="A0-Pluto"))


def test_spec_from_dict_tolerates_and_refuses():
    spec = PdfGenerateSpec.from_dict({"title": "t", "paragraphs": ["a", "b"]})
    assert spec.paragraphs == ["a", "b"] and spec.page_size == "A4"
    with pytest.raises(PdfGenerationError):
        PdfGenerateSpec.from_dict({"paragraphs": "not-a-list"})


# --------------------------------------------------------------------------- #
# 3. Honest degradation                                                        #
# --------------------------------------------------------------------------- #
def test_missing_pypdf_degrades_verbatim(hide_pypdf):
    with pytest.raises(PdfInspectionError) as exc:
        inspect_pdf(_pdf_bytes())
    assert "pdf 支持不可用" in str(exc.value)


def test_missing_fpdf_degrades_verbatim_no_empty_pdf(hide_fpdf):
    with pytest.raises(PdfGenerationError) as exc:
        apply_pdf_spec(PdfGenerateSpec(title="x", paragraphs=["y"]))
    assert "pdf 生成支持不可用" in str(exc.value)


# --------------------------------------------------------------------------- #
# 4. verify tri-state                                                          #
# --------------------------------------------------------------------------- #
def test_verify_pdf_is_tri_state():
    data = _pdf_bytes(paragraph="关键数字 42")
    bare = verify_pdf(data)
    assert bare.openable is True
    assert bare.structure_ok is None and bare.data_ok is None and bare.requirement_ok is None

    ok = verify_pdf(data, {"expected_pages": 1, "max_chars": 100000})
    assert ok.structure_ok is True and ok.requirement_ok is True

    bad = verify_pdf(data, {"expected_pages": 9})
    assert bad.structure_ok is False


def test_verify_pdf_marks_unopenable():
    report = verify_pdf(b"junk")
    assert report.openable is False and report.structure_ok is None
