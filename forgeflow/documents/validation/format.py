"""INC46 T23 — **L3** format fidelity (格式保真：越界零变化 + run 级样式继承).

What L3 checks
--------------
An edit "保真" only if it changes **formatting exactly where it was allowed to**
and nowhere else, and if the text it rewrites **inherits the original run style**.

* **(a) 越界 XML 规范化零变化** — for every paragraph *outside* the located range
  ``[start, end)`` (head ``[0, start)`` and mirror-anchored tail ``[end, len)``)
  the paragraph's **normalised XML** must be unchanged. "Normalised" means the
  Word edit-session stamps (``w:rsid*``) are stripped and the element is
  canonicalised (C14N 2.0) — exactly the comparator discipline used by the T26
  fidelity corpus — so a python-docx re-serialisation (which rewrites bytes,
  timestamps and quotes) does not register as a change, while a real formatting
  change does.
* **(b) run 级样式继承** — for every paragraph *inside* the range whose text
  changed, the **leading run's** run-level attribute snapshot
  (:data:`_RPR_TAGS`: ``w:b`` / ``w:i`` / ``w:color`` / ``w:sz`` / ``w:rFonts``)
  must be identical before and after. This is precisely the ``docx_edit`` writer
  contract (``replace_text`` keeps the first covered run's ``rPr``;
  ``set_paragraph`` writes into the first existing run), stated as a check: the
  edited text inherits the original run style.

Honesty (red line 4 / 15)
-------------------------
Only **measured** attributes are compared. L3 does not guess a "style" and never
turns an unreadable document into a ``pass`` — an unreadable input raises, which
the stack renders as an honest ``fail``.
"""

from __future__ import annotations

import copy
from typing import Any

import lxml.etree as etree
from docx.oxml.ns import qn

from forgeflow.documents.validation.invariants import paragraph_texts
from forgeflow.documents.validation.verdict import (
    FAIL,
    LAYER_FORMAT,
    PASS,
    LayerVerdict,
)

__all__ = [
    "format_verdict",
    "normalized_paragraph_xml",
    "run_rpr_signature",
]

#: The run-level attributes L3 pins (task book: ``w:b`` / ``w:i`` / ``w:color`` /
#: ``w:sz`` / ``w:rFonts``).
_RPR_TAGS: tuple[str, ...] = ("b", "i", "color", "sz", "rFonts")


# --------------------------------------------------------------------------- #
# normalisation                                                                #
# --------------------------------------------------------------------------- #
def _strip_volatile(element: Any) -> None:
    """Remove ``w:rsid*`` attributes in place (Word edit-session stamps)."""
    for node in element.iter():
        for attr in list(node.attrib):
            local = etree.QName(attr).localname if "}" in attr else attr
            if local == "rsid" or local.startswith("rsid"):
                del node.attrib[attr]


def normalized_paragraph_xml(paragraph: Any) -> str:
    """Canonical (C14N 2.0), rsid-stripped XML of a paragraph element.

    The "normalisation" that makes a python-docx re-serialisation compare equal
    while a genuine formatting change does not.
    """
    node = copy.deepcopy(paragraph._p)
    _strip_volatile(node)
    etree.cleanup_namespaces(node)
    return etree.tostring(node, method="c14n2").decode("utf-8")


# --------------------------------------------------------------------------- #
# run-level attribute snapshot                                                 #
# --------------------------------------------------------------------------- #
def run_rpr_signature(run: Any) -> tuple[tuple[str, Any], ...]:
    """The pinned run-level attributes of ``run`` as a comparable tuple.

    ``w:b`` / ``w:i`` are reduced to their effective boolean; ``w:color`` /
    ``w:sz`` to their ``w:val``; ``w:rFonts`` to its sorted attribute set. A run
    without ``w:rPr`` yields ``()`` (default formatting).
    """
    rPr = run._r.find(qn("w:rPr"))
    if rPr is None:
        return ()
    parts: list[tuple[str, Any]] = []
    for tag in _RPR_TAGS:
        element = rPr.find(qn(f"w:{tag}"))
        if element is None:
            continue
        if tag in ("b", "i"):
            value = element.get(qn("w:val"))
            on = value not in ("false", "0", "off") if value is not None else True
            parts.append((tag, on))
        elif tag == "rFonts":
            fonts = tuple(
                sorted(
                    (etree.QName(key).localname if "}" in key else key, val)
                    for key, val in element.attrib.items()
                )
            )
            parts.append((tag, fonts))
        else:
            parts.append((tag, element.get(qn("w:val"))))
    return tuple(parts)


def _first_run_signature(paragraph: Any) -> tuple[tuple[str, Any], ...] | None:
    """The leading run's snapshot, or ``None`` when the paragraph has no run."""
    runs = list(paragraph.runs)
    if not runs:
        return None
    return run_rpr_signature(runs[0])


# --------------------------------------------------------------------------- #
# range helpers                                                                #
# --------------------------------------------------------------------------- #
def _resolve_bounds(length: int, start: int, end: int | None) -> tuple[int, int]:
    lo = max(0, min(int(start), length))
    hi = length if end is None else max(lo, min(int(end), length))
    return lo, hi


# --------------------------------------------------------------------------- #
# verdict                                                                      #
# --------------------------------------------------------------------------- #
def format_verdict(
    before: bytes,
    after: bytes,
    *,
    start: int = 0,
    end: int | None = None,
) -> LayerVerdict:
    """L3 — format-fidelity verdict between ``before`` and ``after``.

    Args:
        before: the original DOCX bytes.
        after: the edited DOCX bytes.
        start: the located range's first paragraph index (inclusive).
        end: the located range's exclusive end paragraph index (``None`` ⇒ EOF).

    Returns:
        A :class:`LayerVerdict`; ``fail`` iff an out-of-range paragraph's
        normalised XML changed, or an edited in-range paragraph's leading run lost
        its run-level formatting.
    """
    from forgeflow.documents.docx_inspect import open_docx

    before_paragraphs = list(open_docx(before).paragraphs)
    after_paragraphs = list(open_docx(after).paragraphs)
    before_texts = paragraph_texts(before)
    after_texts = paragraph_texts(after)

    lo, hi = _resolve_bounds(len(before_paragraphs), start, end)

    out_of_range: list[str] = []
    head = min(lo, len(before_paragraphs), len(after_paragraphs))
    for index in range(head):
        if normalized_paragraph_xml(before_paragraphs[index]) != normalized_paragraph_xml(
            after_paragraphs[index]
        ):
            out_of_range.append(f"paragraph[{index}]")

    tail = min(
        max(0, len(before_paragraphs) - hi),
        max(0, len(after_paragraphs) - hi),
    )
    for offset in range(1, tail + 1):
        if normalized_paragraph_xml(before_paragraphs[-offset]) != normalized_paragraph_xml(
            after_paragraphs[-offset]
        ):
            out_of_range.append(f"paragraph[tail-{offset}]")

    inherited_failures: list[str] = []
    in_range_edited = 0
    in_hi = min(hi, len(before_paragraphs), len(after_paragraphs))
    for index in range(lo, in_hi):
        if before_texts[index] == after_texts[index]:
            continue
        in_range_edited += 1
        before_sig = _first_run_signature(before_paragraphs[index])
        after_sig = _first_run_signature(after_paragraphs[index])
        if before_sig is None:
            # Nothing to inherit (a run-less paragraph): not a fidelity loss.
            continue
        if before_sig != after_sig:
            inherited_failures.append(f"paragraph[{index}]")

    failures = [f"out-of-range:{item}" for item in out_of_range] + [
        f"style-lost:{item}" for item in inherited_failures
    ]
    status = FAIL if failures else PASS
    if failures:
        evidence = "format:FAIL[" + ";".join(failures[:8]) + "]"
    else:
        # An empty denominator is empty *coverage*, not "all inherited": report it
        # as ``n/a`` so ``0/0`` can never be misread as a full pass (红线 4).
        if in_range_edited == 0:
            style_note = "style-inherited=n/a (no in-range edit)"
        else:
            style_note = f"style-inherited={in_range_edited}/{in_range_edited}"
        evidence = (
            f"format:out-of-range=ok({max(0, len(before_paragraphs) - (hi - lo))} paras);"
            f"{style_note}"
        )
    detail: dict[str, Any] = {
        "out_of_range_changes": out_of_range,
        "style_inheritance_failures": inherited_failures,
        "in_range_edited": in_range_edited,
        "range": [lo, hi],
    }
    return LayerVerdict(LAYER_FORMAT, status, evidence, detail)
