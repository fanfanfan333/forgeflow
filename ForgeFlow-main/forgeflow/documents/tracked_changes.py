"""INC46 T22 — 可选输出 ``.tracked.docx``（修订模式 + OOXML 结构校验）。

What it produces
----------------
一个真正的 Word **修订模式**文档：被删内容包在 ``<w:del>``（其 run 用
``<w:delText>``）里，被加内容包在 ``<w:ins>``（其 run 用 ``<w:t>``）里，两者都带
``w:author`` / ``w:date`` / ``w:id``。整段新增 / 删除的段落则把 ``<w:p>`` 直接包进
``<w:ins>`` / ``<w:del>``。author 固定为 :data:`TRACKED_AUTHOR`（``ForgeFlow-Agent``）。

诚实纪律
--------
* 本模块只做**段落级**修订（run 级的字符格式在 diff 里单列）。表格单元格的修订
  不在本版范围，**明确声明**，绝不假装已覆盖。
* :func:`validate_tracked_docx` 做**真正的 OOXML 结构校验**（不是「看起来像」）：
  解压 ``word/document.xml`` → 命名空间内解析 → 逐条检查 ``w:ins`` / ``w:del`` 的
  必备属性与子元素规则（``w:del`` 内必须是 ``w:delText``，``w:ins`` 内必须是
  ``w:t``）→ 最后用 ``python-docx`` **重新打开**证明它是可读的 docx。任一检查
  失败都记入 ``errors`` 并令 ``valid=False``（fail-closed，红线 3）。

纯函数：只读两段字节，绝不写盘、绝不联网。
"""

from __future__ import annotations

import io
import zipfile
import xml.etree.ElementTree as ET
from typing import Any

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from forgeflow.documents.docx_inspect import open_docx

__all__ = [
    "TRACKED_AUTHOR",
    "build_tracked_docx",
    "validate_tracked_docx",
]

#: The author stamped on every ``w:ins`` / ``w:del`` the platform writes.
TRACKED_AUTHOR = "ForgeFlow-Agent"

#: The OOXML (WordprocessingML) namespace + the XML ``space`` attribute namespace.
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_W_INS = f"{{{_W}}}ins"
_W_DEL = f"{{{_W}}}del"
_W_T = f"{{{_W}}}t"
_W_DELTEXT = f"{{{_W}}}delText"
_W_AUTHOR = f"{{{_W}}}author"
_W_DATE = f"{{{_W}}}date"
_W_ID = f"{{{_W}}}id"


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _make_run(text: str, *, deleted: bool) -> Any:
    """A ``<w:r>`` carrying either ``<w:t>`` (insert) or ``<w:delText>`` (delete)."""
    run = OxmlElement("w:r")
    token = OxmlElement("w:delText" if deleted else "w:t")
    token.set(_XML_SPACE, "preserve")
    token.text = text
    run.append(token)
    return run


def _make_marker(tag: str, text: str, *, author: str, date: str, wid: int, deleted: bool) -> Any:
    """A ``<w:ins>`` / ``<w:del>`` element wrapping a single run."""
    marker = OxmlElement(tag)
    marker.set(qn("w:id"), str(wid))
    marker.set(qn("w:author"), author)
    marker.set(qn("w:date"), date)
    marker.append(_make_run(text, deleted=deleted))
    return marker


def _rewrite_paragraph(paragraph: Any, old_text: str, new_text: str, *, author: str, date: str, wid: int) -> None:
    """Mark a modified paragraph: ``<w:del>`` old text, ``<w:ins>`` new text."""
    element = paragraph._p
    # Drop existing runs (keep ``w:pPr`` and any other non-run children).
    for child in list(element):
        if child.tag == qn("w:r"):
            element.remove(child)
    if old_text:
        element.append(_make_marker("w:del", old_text, author=author, date=date, wid=wid, deleted=True))
    if new_text:
        element.append(_make_marker("w:ins", new_text, author=author, date=date, wid=wid + 1, deleted=False))


def _wrap_paragraph(paragraph: Any, tag: str, *, author: str, date: str, wid: int) -> None:
    """Wrap a whole ``<w:p>`` in ``<w:ins>`` / ``<w:del>`` (paragraph-level change)."""
    element = paragraph._p
    parent = element.getparent()
    if parent is None:  # pragma: no cover — a detached paragraph
        return
    wrapper = OxmlElement(tag)
    wrapper.set(qn("w:id"), str(wid))
    wrapper.set(qn("w:author"), author)
    wrapper.set(qn("w:date"), date)
    parent.replace(element, wrapper)
    wrapper.append(element)


def build_tracked_docx(
    old: bytes,
    new: bytes,
    *,
    author: str = TRACKED_AUTHOR,
    date: str | None = None,
) -> bytes:
    """Render ``old → new`` as a Word **tracked-changes** DOCX.

    Only paragraph-level revisions are produced (see the module note). Returns the
    new document bytes (``old`` with ``w:ins`` / ``w:del`` markers applied).

    Raises:
        DocxInspectionError: ``old`` / ``new`` is not a readable DOCX.
    """
    stamp = date or _iso_now()
    document = open_docx(old)
    new_doc = open_docx(new)
    old_paras = list(document.paragraphs)
    new_paras = list(new_doc.paragraphs)
    shared = min(len(old_paras), len(new_paras))

    counter = 1
    for index in range(shared):
        old_text = old_paras[index].text
        new_text = new_paras[index].text
        if old_text == new_text:
            continue
        _rewrite_paragraph(old_paras[index], old_text, new_text, author=author, date=stamp, wid=counter)
        counter += 2

    # Paragraphs that exist only in ``new`` ⇒ inserted paragraphs.
    for index in range(shared, len(new_paras)):
        source = new_paras[index]._p
        cloned = _deep_copy(source)
        element = old_paras[-1]._p if old_paras else None
        parent = element.getparent() if element is not None else None
        if parent is None:
            document.element.body.append(cloned)
        else:
            element.addnext(cloned)
        paragraph = _ParagraphProxy(cloned, parent)
        _wrap_paragraph(paragraph, "w:ins", author=author, date=stamp, wid=counter)
        counter += 1

    # Paragraphs that exist only in ``old`` ⇒ deleted paragraphs.
    for index in range(shared, len(old_paras)):
        _wrap_paragraph(old_paras[index], "w:del", author=author, date=stamp, wid=counter)
        counter += 1

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _deep_copy(element: Any) -> Any:
    """Deep-copy an lxml element (stdlib ``copy.deepcopy`` works on lxml trees)."""
    import copy

    return copy.deepcopy(element)


class _ParagraphProxy:
    """Minimal adapter so :func:`_wrap_paragraph` can wrap a cloned ``w:p``."""

    def __init__(self, element: Any, parent: Any) -> None:
        self._p = element
        self._parent = parent


# --------------------------------------------------------------------------- #
# Structural validation                                                        #
# --------------------------------------------------------------------------- #
def _iter_descendants(root: Any, tag: str) -> list[Any]:
    return [el for el in root.iter(tag)]


def validate_tracked_docx(data: bytes, *, author: str = TRACKED_AUTHOR) -> dict[str, Any]:
    """Structurally validate a tracked-changes DOCX (real OOXML inspection).

    Returns:
        ``{"valid": bool, "checks": [...], "errors": [...],
        "ins_count": int, "del_count": int}``. ``valid`` is ``True`` only when every
        structural rule holds **and** ``python-docx`` can reopen the file.
    """
    checks: list[dict[str, Any]] = []
    errors: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            errors.append(f"{name}: {detail}" if detail else name)

    # 1. It is a zip archive carrying word/document.xml.
    xml_bytes: bytes | None = None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            has_doc = "word/document.xml" in names
            check("zip_has_document_xml", has_doc, "缺少 word/document.xml" if not has_doc else "")
            if has_doc:
                xml_bytes = archive.read("word/document.xml")
    except zipfile.BadZipFile as exc:
        check("is_zip_archive", False, f"不是合法 zip：{exc}")

    if xml_bytes is None:
        return {"valid": False, "checks": checks, "errors": errors, "ins_count": 0, "del_count": 0}

    # 2. document.xml parses and declares the WordprocessingML namespace.
    try:
        root = ET.fromstring(xml_bytes)
        check("document_xml_parses", True)
    except ET.ParseError as exc:
        check("document_xml_parses", False, f"XML 解析失败：{exc}")
        return {"valid": False, "checks": checks, "errors": errors, "ins_count": 0, "del_count": 0}

    check("declares_w_namespace", root.tag.startswith(f"{{{_W}}}"), root.tag)

    ins_elements = _iter_descendants(root, _W_INS)
    del_elements = _iter_descendants(root, _W_DEL)
    check("has_tracked_markers", bool(ins_elements or del_elements),
          f"ins={len(ins_elements)} del={len(del_elements)}")

    # 3. Every marker carries author + date; the author matches the platform's.
    for element in ins_elements + del_elements:
        tag = "ins" if element.tag == _W_INS else "del"
        marker_author = element.get(_W_AUTHOR)
        check(f"{tag}_has_author", bool(marker_author), f"<w:{tag}> 缺少 w:author")
        if marker_author:
            check(f"{tag}_author_is_agent", marker_author == author, f"{marker_author!r} != {author!r}")
        check(f"{tag}_has_date", bool(element.get(_W_DATE)), f"<w:{tag}> 缺少 w:date")
        check(f"{tag}_has_id", bool(element.get(_W_ID)), f"<w:{tag}> 缺少 w:id")

    # 4. ``w:del`` runs must carry ``w:delText`` (never ``w:t``); ``w:ins`` the reverse.
    for element in del_elements:
        del_texts = _iter_descendants(element, _W_DELTEXT)
        stray_t = _iter_descendants(element, _W_T)
        check("del_uses_deltext", bool(del_texts), "<w:del> 内没有 <w:delText>")
        check("del_has_no_plain_text", not stray_t, "<w:del> 内不得出现 <w:t>")
    for element in ins_elements:
        plain_t = _iter_descendants(element, _W_T)
        check("ins_uses_text", bool(plain_t), "<w:ins> 内没有 <w:t>")

    # 5. python-docx can reopen it (the strongest "it is still a real DOCX" check).
    try:
        Document(io.BytesIO(data))
        check("reopens_with_python_docx", True)
    except Exception as exc:  # noqa: BLE001 — any reopen failure invalidates the artifact
        check("reopens_with_python_docx", False, f"python-docx 无法打开：{exc}")

    return {
        "valid": not errors,
        "checks": checks,
        "errors": errors,
        "ins_count": len(ins_elements),
        "del_count": len(del_elements),
    }
