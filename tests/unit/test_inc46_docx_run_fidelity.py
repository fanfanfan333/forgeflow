"""INC46 — DOCX ``replace_text`` run-level formatting fidelity (regression nails).

The defect this file pins down
------------------------------
``docx_edit._set_text_preserving`` rewrote the **whole** paragraph into its first
run and blanked the rest. For a paragraph made of several differently formatted
runs ("多 run 混排") that flattened the mixed run formatting: ``runs[0].text``
received the entire new paragraph and ``runs[1:]`` were emptied. QA reproduced it
through the platform chain; this file reproduces it at the unit layer and pins the
fix.

What is asserted
----------------
1. a ``replace_text`` match fully inside one run changes **only** that run;
2. every sibling run keeps its ``text`` **and** its run-level properties
   (``bold`` / ``italic`` / ``underline`` / ``font`` / ``size`` / ``colour`` /
   ``highlight``);
3. a match spanning runs merges only the covered runs and adopts the **first**
   covered run's format, leaving the runs before / after it untouched;
4. the run-level ``w:rPr`` XML is compared **byte-for-byte**, not merely re-built
   and assumed equivalent;
5. zero hits write no bytes; the run-flattening shape never reappears.
"""

from __future__ import annotations

import hashlib
import io

from docx import Document
from docx.enum.text import WD_COLOR_INDEX
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

from forgeflow.documents import EditOp, apply_edits

#: The QA fixture's enriched run: every character-format dimension set at once.
_RPR_STYLE = {
    "bold": True,
    "italic": True,
    "underline": True,
    "font": "Times New Roman",
    "size": Pt(14),
    "color": RGBColor(0xC0, 0x00, 0x00),
    "highlight": WD_COLOR_INDEX.YELLOW,
}


# --------------------------------------------------------------------------- #
# Builders / helpers                                                           #
# --------------------------------------------------------------------------- #
def _apply_rpr(run, style: dict) -> None:
    """Set every element of ``style`` onto ``run`` (subset of ``_RPR_STYLE``)."""
    if "bold" in style:
        run.bold = style["bold"]
    if "italic" in style:
        run.italic = style["italic"]
    if "underline" in style:
        run.underline = style["underline"]
    if "font" in style:
        run.font.name = style["font"]
    if "size" in style:
        run.font.size = style["size"]
    if "color" in style:
        run.font.color.rgb = style["color"]
    if "highlight" in style:
        run.font.highlight_color = style["highlight"]


def _rich_run(paragraph, text: str):
    """A fully formatted run (all dimensions of :data:`_RPR_STYLE`)."""
    run = paragraph.add_run(text)
    _apply_rpr(run, _RPR_STYLE)
    return run


def _props(run) -> dict:
    """The observable character-format attributes of ``run`` (JSON-safe)."""
    color = run.font.color
    return {
        "text": run.text,
        "bold": run.bold,
        "italic": run.italic,
        "underline": run.underline,
        "font": run.font.name,
        "size": None if run.font.size is None else run.font.size.pt,
        "color": None if (color is None or color.rgb is None) else str(color.rgb),
        "highlight": None
        if run.font.highlight_color is None
        else str(run.font.highlight_color),
    }


def _rpr_xml(run) -> str | None:
    """The run's ``<w:rPr>`` element serialised, or ``None`` when it has none."""
    rpr = run._r.find(qn("w:rPr"))
    return None if rpr is None else rpr.xml


def _save(document) -> bytes:
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _first_paragraph(data: bytes):
    return Document(io.BytesIO(data)).paragraphs[0]


# --------------------------------------------------------------------------- #
# 1. A match inside one run changes only that run                              #
# --------------------------------------------------------------------------- #
def test_replace_within_one_run_leaves_sibling_runs_untouched():
    """The exact QA fixture: a rich run + a plain run; replace inside the rich one."""
    document = Document()
    paragraph = document.add_paragraph()
    rich = _rich_run(paragraph, "总金额 40 元")
    plain = paragraph.add_run("，备注：请核对。")
    source = _save(document)

    rich_props_before = _props(rich)
    plain_props_before = _props(plain)
    plain_rpr_before = _rpr_xml(plain)

    new, changes = apply_edits(
        source, [EditOp(op="replace_text", match="40", replace="38")]
    )

    assert changes == 1
    after = _first_paragraph(new)
    rich_after, plain_after = after.runs[0], after.runs[1]

    # Only the matched run's text moved …
    assert rich_after.text == "总金额 38 元"
    assert rich_after.text != rich_props_before["text"]
    # … the sibling run is byte-for-byte the very same run …
    assert plain_after.text == plain_props_before["text"] == "，备注：请核对。"
    assert _rpr_xml(plain_after) == plain_rpr_before
    assert _props(plain_after) == plain_props_before
    # … and the matched run keeps every format dimension.
    assert _props(rich_after) == {**rich_props_before, "text": "总金额 38 元"}


def test_replace_inside_the_plain_run_leaves_the_formatted_run_untouched():
    """The symmetric case: a match inside the plain run must not touch the rich one."""
    document = Document()
    paragraph = document.add_paragraph()
    rich = _rich_run(paragraph, "总金额 40 元")
    paragraph.add_run("，备注：请核对。")
    source = _save(document)

    rich_props_before = _props(rich)
    rich_rpr_before = _rpr_xml(rich)

    new, changes = apply_edits(
        source, [EditOp(op="replace_text", match="备注", replace="说明")]
    )

    assert changes == 1
    after = _first_paragraph(new)
    assert after.runs[0].text == "总金额 40 元"
    assert after.runs[1].text == "，说明：请核对。"
    # The rich run is untouched — text, all attributes, and its raw ``w:rPr``.
    assert _props(after.runs[0]) == rich_props_before
    assert _rpr_xml(after.runs[0]) == rich_rpr_before


# --------------------------------------------------------------------------- #
# 2. Every rPr dimension is preserved (byte-for-byte on the raw rPr)           #
# --------------------------------------------------------------------------- #
def test_all_rpr_dimensions_survive_a_sibling_edit():
    """Bold / italic / underline / font / size / colour / highlight — each checked."""
    document = Document()
    paragraph = document.add_paragraph()
    rich = _rich_run(paragraph, "金额 40")
    tail = paragraph.add_run(" 元。")
    tail.bold = False  # give the sibling a *different*, explicit rPr too
    source = _save(document)

    rich_rpr_before = _rpr_xml(rich)
    tail_rpr_before = _rpr_xml(tail)

    new, _ = apply_edits(source, [EditOp(op="replace_text", match="40", replace="99")])
    after = _first_paragraph(new)

    assert after.runs[0].text == "金额 99"
    assert after.runs[1].text == " 元。"
    # Raw rPr XML is literally identical for both runs (no rebuild drift).
    assert _rpr_xml(after.runs[0]) == rich_rpr_before
    assert _rpr_xml(after.runs[1]) == tail_rpr_before
    # And each dimension is observable on the reopened run.
    props = _props(after.runs[0])
    assert props["bold"] is True
    assert props["italic"] is True
    assert props["underline"] is True
    assert props["font"] == "Times New Roman"
    assert props["size"] == 14.0
    assert props["color"] == "C00000"
    assert props["highlight"] == "YELLOW (7)"


# --------------------------------------------------------------------------- #
# 3. A cross-run match merges only the covered runs                            #
# --------------------------------------------------------------------------- #
def test_cross_run_match_merges_only_covered_runs_and_keeps_first_format():
    """``ABCD`` spans run0 + run1 → merged into run0 (its format), run2 untouched."""
    document = Document()
    paragraph = document.add_paragraph()
    run0 = paragraph.add_run("前缀ABC")
    run0.bold = True
    run1 = paragraph.add_run("DEF后缀")
    run1.italic = True
    run2 = paragraph.add_run("（注）")
    run2.underline = True
    source = _save(document)

    run1_rpr_before = _rpr_xml(run1)
    run2_props_before = _props(run2)
    run2_rpr_before = _rpr_xml(run2)

    # ``ABCD`` = offset 2..6 → first char in run0, last char in run1.
    new, changes = apply_edits(
        source, [EditOp(op="replace_text", match="ABCD", replace="XY")]
    )

    assert changes == 1
    after = _first_paragraph(new)
    assert after.text == "前缀XYEF后缀（注）"
    # Merged into the *first* covered run, which keeps its own format.
    assert after.runs[0].text == "前缀XY"
    assert after.runs[0].bold is True
    # The last covered run keeps only the text after the match, and its rPr.
    assert after.runs[1].text == "EF后缀"
    assert _rpr_xml(after.runs[1]) == run1_rpr_before
    # The run after the match is untouched.
    assert after.runs[2].text == "（注）"
    assert _props(after.runs[2]) == run2_props_before
    assert _rpr_xml(after.runs[2]) == run2_rpr_before


def test_cross_run_match_that_consumes_a_whole_middle_run_blanks_it():
    """A match covering run1 entirely blanks it but never touches run2/its rPr."""
    document = Document()
    paragraph = document.add_paragraph()
    paragraph.add_run("A").bold = True
    paragraph.add_run("BB").italic = True
    tail = paragraph.add_run("尾")
    tail.underline = True
    source = _save(document)

    tail_rpr_before = _rpr_xml(tail)

    new, changes = apply_edits(
        source, [EditOp(op="replace_text", match="AB", replace="Z")]
    )

    assert changes == 1
    after = _first_paragraph(new)
    assert after.text == "ZB尾"
    assert after.runs[0].text == "Z" and after.runs[0].bold is True
    assert after.runs[1].text == "B"  # one matched char removed from the middle run
    assert after.runs[2].text == "尾"
    assert _rpr_xml(after.runs[2]) == tail_rpr_before


# --------------------------------------------------------------------------- #
# 4. Zero-hit and multi-hit semantics are unchanged                            #
# --------------------------------------------------------------------------- #
def test_no_match_writes_no_bytes():
    document = Document()
    paragraph = document.add_paragraph()
    _rich_run(paragraph, "金额 40")
    paragraph.add_run(" 元。")
    source = _save(document)

    new, changes = apply_edits(
        source, [EditOp(op="replace_text", match="不存在", replace="x")]
    )

    assert changes == 0
    assert hashlib.sha256(new).hexdigest() == hashlib.sha256(source).hexdigest()


def test_all_occurrences_in_one_run_are_replaced():
    document = Document()
    paragraph = document.add_paragraph()
    run = paragraph.add_run("40 和 40")
    run.bold = True
    paragraph.add_run("。")
    source = _save(document)

    new, changes = apply_edits(
        source, [EditOp(op="replace_text", match="40", replace="7")]
    )

    assert changes == 2
    after = _first_paragraph(new)
    assert after.runs[0].text == "7 和 7"
    assert after.runs[0].bold is True
    assert after.runs[1].text == "。"


def test_never_flattens_a_multi_run_paragraph():
    """The regression shape: runs[0] must NOT become the whole paragraph text."""
    document = Document()
    paragraph = document.add_paragraph()
    _rich_run(paragraph, "总金额 40 元")
    paragraph.add_run("，备注：请核对。")
    source = _save(document)

    new, _ = apply_edits(source, [EditOp(op="replace_text", match="40", replace="38")])
    after = _first_paragraph(new)

    whole = "总金额 38 元，备注：请核对。"
    # The buggy output (run[0] == whole paragraph, run[1] == "") is gone.
    assert after.runs[0].text != whole
    assert after.runs[1].text != ""
    assert after.runs[1].text == "，备注：请核对。"
    # And the paragraph's visible text is still correct.
    assert after.text == whole
