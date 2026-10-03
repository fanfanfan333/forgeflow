"""T26 保真语料库（Fidelity Corpus）— 语料构建器。

本模块 **合成** ≥40 份真实结构 DOCX，覆盖 TABLE 25 要求的 15 类特征（每类 ≥2 份），
并生成特征矩阵 ``tests/corpus/manifest.yaml``。所有样本均为程序化合成，
**不含任何真实客户数据**（红线 16）。

为什么需要它
------------
既有 12 例保真样本覆盖不了批注 / 修订 / 域 / 脚注 / 合并单元格 / 嵌套表格 /
文本框 / 浮动图片 / 页眉页脚分节 / 多级编号 / 内容控件 / 超链接书签 / 公式 /
样式主题 / 大文档这些真实结构。没有语料，「某特征无样本 ⇒ 空覆盖」会伪装成
「全绿」。本构建器用 python-docx 创作原生结构，用直接 OOXML 手术（lxml 改
``word/document.xml``、补 part、写 rels / ``[Content_Types].xml``）覆盖
python-docx 不能原生创作的特征。

构建两段往返所需的「目标段落」
------------------------------
每份语料都含一个 **body 级目标段落**，其中嵌唯一标记 ``[[TGT::<slug>]]``。
定向编辑往返用平台的 ``replace_text`` 把该标记替换为 ``已编辑::<slug>``，
据此断言「仅目标段落变化，其余逐字节不变」。

确定性
------
为保证语料字节可复现（清单哈希稳定），本模块：
  * 把 ``docProps/core.xml`` 的时间戳固定为 ``2026-10-03T00:00:00Z``；
  * 用固定 ``date_time`` 的确定性 zip 重打包。

用法::

    python tests/corpus/build_corpus.py            # 生成语料 + manifest
    python tests/corpus/build_corpus.py --verify    # 生成后自检（可打开 / 计数）
"""

from __future__ import annotations

import hashlib
import io
import struct
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import lxml.etree as etree

# --------------------------------------------------------------------------- #
# 命名空间                                                                    #
# --------------------------------------------------------------------------- #
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
V_NS = "urn:schemas-microsoft-com:vml"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
PIC_NS = "http://schemas.openxmlformats.org/drawingml/2006/picture"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
XML_NS = "http://www.w3.org/XML/1998/namespace"
DCT_NS = "http://purl.org/dc/terms/"

_NS = {
    "w": W_NS,
    "r": R_NS,
    "m": M_NS,
    "v": V_NS,
    "wp": WP_NS,
    "a": A_NS,
    "pic": PIC_NS,
    "ct": CT_NS,
    "rel": REL_NS,
    "xml": XML_NS,
}

_REL_BASE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
_CT_BASE = "application/vnd.openxmlformats-officedocument.wordprocessingml."


def _q(tag: str) -> str:
    """``"w:p"`` → Clark 记法 ``"{<uri>}p"``。"""
    prefix, _, local = tag.partition(":")
    return f"{{{_NS[prefix]}}}{local}"


def _el(parent: etree._Element, tag: str, **attrs: str) -> etree._Element:
    """在 ``parent`` 下新建 ``tag``（支持前缀），并设置属性。

    属性名含 ``:``（如 ``w:val``）按命名空间解析；否则视为普通属性（如
    ``relativeFrom``）。
    """
    element = etree.SubElement(parent, _q(tag))
    for key, value in attrs.items():
        if key.startswith("{"):
            element.set(key, value)
        elif ":" in key:
            element.set(_q(key), value)
        else:
            element.set(key, value)
    return element


def _text(element: etree._Element, value: str) -> etree._Element:
    element.text = value
    return element


def _run(parent: etree._Element, text: str) -> etree._Element:
    run = _el(parent, "w:r")
    t = _el(run, "w:t", **{"xml:space": "preserve"})
    _text(t, text)
    return run


# --------------------------------------------------------------------------- #
# 确定性 zip / 包操作                                                          #
# --------------------------------------------------------------------------- #
class _Package:
    """一个可增改 part 的 DOCX 包（用于直接 OOXML 手术）。"""

    def __init__(self, data: bytes) -> None:
        self.parts: dict[str, bytes] = {}
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for name in archive.namelist():
                self.parts[name] = archive.read(name)

    def xml(self, name: str) -> etree._Element:
        return etree.fromstring(self.parts[name])

    def set_xml(self, name: str, root: etree._Element) -> None:
        self.parts[name] = etree.tostring(
            root, xml_declaration=True, encoding="UTF-8", standalone=True
        )

    def add_part(self, name: str, blob: bytes) -> None:
        self.parts[name] = blob

    def add_content_type(self, part_name: str, content_type: str) -> None:
        root = self.xml("[Content_Types].xml")
        for child in root:
            if child.get("PartName") == part_name:
                return
        override = etree.SubElement(root, _q("ct:Override"))
        override.set("PartName", part_name)
        override.set("ContentType", content_type)
        self.set_xml("[Content_Types].xml", root)

    def add_relationship(
        self, rels_name: str, rid: str, rel_type: str, target: str, mode: str | None = None
    ) -> None:
        root = self.xml(rels_name)
        for child in root:
            if child.get("Id") == rid:
                return
        rel = etree.SubElement(root, _q("rel:Relationship"))
        rel.set("Id", rid)
        rel.set("Type", rel_type)
        rel.set("Target", target)
        if mode:
            rel.set("TargetMode", mode)
        self.set_xml(rels_name, root)

    def to_bytes(self) -> bytes:
        return _deterministic_zip(self.parts)


def _deterministic_zip(parts: dict[str, bytes]) -> bytes:
    """按名字排序、固定时间戳重打包 → 字节可复现。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(parts):
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 3, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, parts[name])
    return buffer.getvalue()


def _normalize_core(pkg: _Package) -> None:
    """把 ``docProps/core.xml`` 的时间戳固定，去掉生成时刻带来的抖动。"""
    if "docProps/core.xml" not in pkg.parts:
        return
    root = etree.fromstring(pkg.parts["docProps/core.xml"])
    for tag in ("created", "modified"):
        element = root.find(f"{{{DCT_NS}}}{tag}")
        if element is not None:
            element.text = "2026-10-03T00:00:00Z"
    pkg.set_xml("docProps/core.xml", root)


# --------------------------------------------------------------------------- #
# 合成素材                                                                    #
# --------------------------------------------------------------------------- #
def _png(width: int = 16, height: int = 16, rgb: tuple[int, int, int] = (0x2E, 0x86, 0xC1)) -> bytes:
    """生成一张最小的合法 PNG（确定性），供浮动图片特征使用。"""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib_crc(body))

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        signature
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", _zlib_compress(raw))
        + chunk(b"IEND", b"")
    )


# 单独封装，避免顶部 import 污染命名空间
def zlib_crc(data: bytes) -> int:
    import zlib

    return zlib.crc32(data) & 0xFFFFFFFF


def _zlib_compress(data: bytes) -> bytes:
    import zlib

    return zlib.compress(data, 9)


_PNG = _png()


# --------------------------------------------------------------------------- #
# 目标段落（每份语料都含）                                                     #
# --------------------------------------------------------------------------- #
def _marker(slug: str) -> str:
    return f"[[TGT::{slug}]]"


def _replacement(slug: str) -> str:
    return f"已编辑::{slug}"


def _base_doc(spec: "Spec") -> Any:
    """构造所有语料共用的骨架：标题 + 前言 + 目标段落 + 收尾段落。"""
    from docx import Document

    document = Document()
    document.add_heading(spec.title, level=1)
    document.add_paragraph(f"文档编号：{spec.slug}；用途：{spec.notes}。")
    document.add_paragraph(
        "本段的占位标记为 "
        f"{_marker(spec.slug)}，金额 1,000.00 元，日期 2026-10-03，"
        "定向编辑只应改动这一段。"
    )
    document.add_heading("正文小节", level=2)
    document.add_paragraph("这是正文小节的一段普通文本，用于承载拟真结构。")
    document.add_paragraph("收尾段落：用于验证未被编辑的段落逐字节不变。")
    return document


def _finalize(spec: "Spec", native: Callable[[Any, "Spec"], None] | None,
              surgery: Callable[[_Package, "Spec"], None] | None) -> bytes:
    document = _base_doc(spec)
    if native is not None:
        native(document, spec)
    buffer = io.BytesIO()
    document.save(buffer)
    pkg = _Package(buffer.getvalue())
    if surgery is not None:
        surgery(pkg, spec)
    pkg.parts["word/document.xml"] = etree.tostring(
        pkg.xml("word/document.xml"), xml_declaration=True, encoding="UTF-8", standalone=True
    )
    _normalize_core(pkg)
    return pkg.to_bytes()


# --------------------------------------------------------------------------- #
# document.xml 手术工具                                                        #
# --------------------------------------------------------------------------- #
def _body(root: etree._Element) -> etree._Element:
    body = root.find(_q("w:body"))
    assert body is not None, "document.xml 缺少 w:body"
    return body


def _body_paragraphs(root: etree._Element) -> list[etree._Element]:
    body = _body(root)
    return [c for c in body if etree.QName(c).localname == "p"]


def _paragraph_containing(root: etree._Element, needle: str) -> etree._Element:
    for para in _body_paragraphs(root):
        text = "".join(t.text or "" for t in para.iter(_q("w:t")))
        if needle in text:
            return para
    raise KeyError(f"找不到含 {needle!r} 的段落")


def _comment_reference_run(comment_id: str) -> etree._Element:
    run = etree.Element(_q("w:r"))
    rpr = _el(run, "w:rPr")
    _el(rpr, "w:rStyle", **{"w:val": "CommentReference"})
    _el(run, "w:commentReference", **{"w:id": comment_id})
    return run


# --------------------------------------------------------------------------- #
# 15 类特征的构建器                                                            #
# --------------------------------------------------------------------------- #
def _surgery_comments(pkg: _Package, spec: "Spec") -> None:
    cid = str(1 + spec.variant)
    comments = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:comments xmlns:w="{W_NS}">'
        f'<w:comment w:id="{cid}" w:author="复核人" w:initials="FH" '
        f'w:date="2026-10-03T00:00:00Z"><w:p><w:r>'
        f'<w:t>批注{cid}：请核对金额与日期，勿改动表格。</w:t>'
        "</w:r></w:p></w:comment></w:comments>"
    ).encode("utf-8")
    pkg.add_part("word/comments.xml", comments)
    pkg.add_content_type("/word/comments.xml", _CT_BASE + "comments+xml")
    pkg.add_relationship(
        "word/_rels/document.xml.rels", f"rIdComment{cid}", _REL_BASE + "comments", "comments.xml"
    )

    root = pkg.xml("word/document.xml")
    para = _paragraph_containing(root, "这是正文小节的一段普通文本")
    start = etree.Element(_q("w:commentRangeStart"))
    start.set(_q("w:id"), cid)
    end = etree.Element(_q("w:commentRangeEnd"))
    end.set(_q("w:id"), cid)
    first_run = para.find(_q("w:r"))
    first_run.addprevious(start)
    first_run.addnext(end)
    end.addnext(_comment_reference_run(cid))
    pkg.set_xml("word/document.xml", root)


def _surgery_revisions(pkg: _Package, spec: "Spec") -> None:
    root = pkg.xml("word/document.xml")
    para = _paragraph_containing(root, "这是正文小节的一段普通文本")
    n = str(spec.variant + 1)
    ins = etree.Element(_q("w:ins"))
    ins.set(_q("w:id"), str(100 + spec.variant))
    ins.set(_q("w:author"), "修订人")
    ins.set(_q("w:date"), "2026-10-03T00:00:00Z")
    _run(ins, f"新增内容{n}")
    dele = etree.Element(_q("w:del"))
    dele.set(_q("w:id"), str(200 + spec.variant))
    dele.set(_q("w:author"), "修订人")
    dele.set(_q("w:date"), "2026-10-03T00:00:00Z")
    del_run = _el(dele, "w:r")
    _el(del_run, "w:delText", **{"xml:space": "preserve"})
    del_run.find(_q("w:delText")).text = f"删除内容{n}"
    para.append(ins)
    para.append(dele)
    pkg.set_xml("word/document.xml", root)


def _surgery_fields(pkg: _Package, spec: "Spec") -> None:
    root = pkg.xml("word/document.xml")
    body = _body(root)
    # 简单域：PAGE
    simple_p = etree.Element(_q("w:p"))
    fld = etree.Element(_q("w:fldSimple"))
    fld.set(_q("w:instr"), " PAGE ")
    _run(fld, "1")
    simple_p.append(fld)
    body.append(simple_p)
    # 复杂域：TOC（begin / instrText / separate / result / end）
    complex_p = etree.Element(_q("w:p"))
    for kind, instr in (("begin", None), (None, " TOC \\o \"1-3\" \\h "), ("separate", None)):
        run = _el(complex_p, "w:r")
        if kind is not None:
            _el(run, "w:fldChar", **{"w:fldCharType": kind})
        else:
            assert instr is not None
            it = _el(run, "w:instrText", **{"xml:space": "preserve"})
            it.text = instr
    _run(complex_p, "目录占位：第 1 章 …")
    end_run = _el(complex_p, "w:r")
    _el(end_run, "w:fldChar", **{"w:fldCharType": "end"})
    body.append(complex_p)
    # 交叉引用域
    ref_p = etree.Element(_q("w:p"))
    _run(ref_p, "见")
    ref_run = _el(ref_p, "w:r")
    ref_instr = _el(ref_run, "w:instrText", **{"xml:space": "preserve"})
    ref_instr.text = " REF _Ref100 \\h "
    _run(ref_p, "图 1")
    body.append(ref_p)
    pkg.set_xml("word/document.xml", root)


def _footnote_xml(spec: "Spec") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:footnotes xmlns:w="{W_NS}">'
        '<w:footnote w:id="-1" w:type="separator"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
        '<w:footnote w:id="0" w:type="continuationSeparator"><w:p><w:r>'
        "<w:continuationSeparator/></w:r></w:p></w:footnote>"
        '<w:footnote w:id="1"><w:p><w:r><w:rPr><w:rStyle w:val="FootnoteReference"/>'
        '</w:rPr><w:footnoteRef/></w:r><w:r><w:t>脚注 1：数据来源为内部台账。'
        "</w:t></w:r></w:p></w:footnote></w:footnotes>"
    ).encode("utf-8")


def _endnote_xml(spec: "Spec") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:endnotes xmlns:w="{W_NS}">'
        '<w:endnote w:id="-1" w:type="separator"><w:p><w:r><w:separator/></w:r></w:p></w:endnote>'
        '<w:endnote w:id="0" w:type="continuationSeparator"><w:p><w:r>'
        "<w:continuationSeparator/></w:r></w:p></w:endnote>"
        '<w:endnote w:id="1"><w:p><w:r><w:rPr><w:rStyle w:val="EndnoteReference"/>'
        '</w:rPr><w:endnoteRef/></w:r><w:r><w:t>尾注 1：口径见附录。'
        "</w:t></w:r></w:p></w:endnote></w:endnotes>"
    ).encode("utf-8")


def _surgery_notes(pkg: _Package, spec: "Spec") -> None:
    pkg.add_part("word/footnotes.xml", _footnote_xml(spec))
    pkg.add_content_type("/word/footnotes.xml", _CT_BASE + "footnotes+xml")
    pkg.add_relationship(
        "word/_rels/document.xml.rels", "rIdFootnote1", _REL_BASE + "footnotes", "footnotes.xml"
    )
    pkg.add_part("word/endnotes.xml", _endnote_xml(spec))
    pkg.add_content_type("/word/endnotes.xml", _CT_BASE + "endnotes+xml")
    pkg.add_relationship(
        "word/_rels/document.xml.rels", "rIdEndnote1", _REL_BASE + "endnotes", "endnotes.xml"
    )
    root = pkg.xml("word/document.xml")
    para = _paragraph_containing(root, "这是正文小节的一段普通文本")
    fn_run = _el(para, "w:r")
    fn_rpr = _el(fn_run, "w:rPr")
    _el(fn_rpr, "w:rStyle", **{"w:val": "FootnoteReference"})
    _el(fn_run, "w:footnoteReference", **{"w:id": "1"})
    en_run = _el(para, "w:r")
    en_rpr = _el(en_run, "w:rPr")
    _el(en_rpr, "w:rStyle", **{"w:val": "EndnoteReference"})
    _el(en_run, "w:endnoteReference", **{"w:id": "1"})
    pkg.set_xml("word/document.xml", root)


def _native_merged_cells(document: Any, spec: "Spec") -> None:
    table = document.add_table(rows=3, cols=3)
    table.style = "Table Grid"
    table.cell(0, 0).text = "横向合并"
    table.cell(0, 0).merge(table.cell(0, 2))
    table.cell(1, 0).text = "纵向合并"
    table.cell(1, 0).merge(table.cell(2, 0))
    for r in range(1, 3):
        for c in range(1, 3):
            table.cell(r, c).text = f"单元格{r}{c}"


def _native_nested_tables(document: Any, spec: "Spec") -> None:
    outer = document.add_table(rows=2, cols=2)
    outer.style = "Table Grid"
    outer.cell(0, 0).text = "外层左上"
    inner = outer.cell(0, 1).add_table(rows=2, cols=2)
    inner.style = "Table Grid"
    for r in range(2):
        for c in range(2):
            inner.cell(r, c).text = f"内层{r}{c}"
    outer.cell(1, 0).text = "外层左下"
    outer.cell(1, 1).text = "外层右下"


def _surgery_textboxes(pkg: _Package, spec: "Spec") -> None:
    root = pkg.xml("word/document.xml")
    para = _paragraph_containing(root, "这是正文小节的一段普通文本")
    run = etree.Element(_q("w:r"))
    pict = _el(run, "w:pict")
    shape = etree.SubElement(pict, _q("v:shape"))
    shape.set("style", "width:200pt;height:60pt")
    shape.set("type", "#_x0000_t202")
    textbox = etree.SubElement(shape, _q("v:textbox"))
    txbx = _el(textbox, "w:txbxContent")
    inner = _el(txbx, "w:p")
    _run(inner, f"文本框内容 {spec.slug}：形状内文本随文本块一并保留。")
    para.append(run)
    pkg.set_xml("word/document.xml", root)


def _native_floating_image(document: Any, spec: "Spec") -> None:
    document.add_picture(io.BytesIO(_PNG))


def _float_the_drawing(pkg: _Package) -> None:
    root = pkg.xml("word/document.xml")
    for inline in list(root.iter(_q("wp:inline"))):
        parent = inline.getparent()
        anchor = etree.Element(_q("wp:anchor"))
        for attr, val in (
            ("distT", "0"), ("distB", "0"), ("distL", "0"), ("distR", "0"),
            ("simplePos", "0"), ("relativeHeight", "251658240"), ("behindDoc", "0"),
            ("locked", "0"), ("layoutInCell", "1"), ("allowOverlap", "1"),
        ):
            anchor.set(attr, val)
        pos_h = _el(anchor, "wp:positionH", relativeFrom="column")
        _text(_el(pos_h, "wp:posOffset"), "0")
        pos_v = _el(anchor, "wp:positionV", relativeFrom="paragraph")
        _text(_el(pos_v, "wp:posOffset"), "0")
        simple_pos = etree.Element(_q("wp:simplePos"))
        simple_pos.set("x", "0")
        simple_pos.set("y", "0")
        anchor.insert(0, simple_pos)
        children = list(inline)
        # extent / effectExtent 先放，然后是 wrapNone，再是 docPr 等
        extent = [c for c in children if etree.QName(c).localname in ("extent", "effectExtent")]
        rest = [c for c in children if c not in extent]
        for child in extent:
            anchor.append(child)
        _el(anchor, "wp:wrapNone")
        for child in rest:
            anchor.append(child)
        parent.replace(inline, anchor)
    pkg.set_xml("word/document.xml", root)


def _native_headers_footers(document: Any, spec: "Spec") -> None:
    from docx.enum.section import WD_SECTION

    def _ensure_para(part: Any) -> Any:
        if not part.paragraphs:
            return part.add_paragraph()
        return part.paragraphs[0]

    first = document.sections[0]
    _ensure_para(first.header).text = f"页眉（第 1 节）：{spec.slug}"
    _ensure_para(first.footer).text = "页脚（第 1 节）：第 X 页，共 Y 页"
    second = document.add_section(WD_SECTION.NEW_PAGE)
    second.header.is_linked_to_previous = False
    second.footer.is_linked_to_previous = False
    _ensure_para(second.header).text = f"页眉（第 2 节）：{spec.slug} · 附录"
    _ensure_para(second.footer).text = "页脚（第 2 节）：机密"


def _native_numbering(document: Any, spec: "Spec") -> None:
    levels = ["List Number", "List Number 2", "List Number 3", "List Bullet"]
    for index in range(6):
        paragraph = document.add_paragraph(
            f"多级列表项 {index + 1}（级别 {index % 3 + 1}）", style=levels[index % len(levels)]
        )
        paragraph.paragraph_format.left_indent = None


def _surgery_content_controls(pkg: _Package, spec: "Spec") -> None:
    root = pkg.xml("word/document.xml")
    body = _body(root)
    sdt = etree.Element(_q("w:sdt"))
    pr = _el(sdt, "w:sdtPr")
    _el(pr, "w:alias", **{"w:val": "合同编号"})
    _el(pr, "w:tag", **{"w:val": f"contractNo{spec.variant}"})
    _el(pr, "w:id", **{"w:val": str(900 + spec.variant)})
    _el(pr, "w:text")
    content = _el(sdt, "w:sdtContent")
    inner = _el(content, "w:p")
    _run(inner, f"内容控件受控文本 {spec.slug}")
    body.append(sdt)
    # 块级内容控件包裹一张小表
    sdt2 = etree.Element(_q("w:sdt"))
    pr2 = _el(sdt2, "w:sdtPr")
    _el(pr2, "w:alias", **{"w:val": "附件表"})
    c2 = _el(sdt2, "w:sdtContent")
    tbl = _el(c2, "w:tbl")
    for r in range(2):
        tr = _el(tbl, "w:tr")
        for c in range(2):
            tc = _el(tr, "w:tc")
            p = _el(tc, "w:p")
            _run(p, f"CC表格{r}{c}")
    body.append(sdt2)
    pkg.set_xml("word/document.xml", root)


def _surgery_hyperlinks(pkg: _Package, spec: "Spec") -> None:
    pkg.add_relationship(
        "word/_rels/document.xml.rels",
        f"rIdLink{spec.variant}",
        _REL_BASE + "hyperlink",
        "https://example.com/forgeflow",
        mode="External",
    )
    root = pkg.xml("word/document.xml")
    para = _paragraph_containing(root, "这是正文小节的一段普通文本")
    bm_start = etree.Element(_q("w:bookmarkStart"))
    bm_start.set(_q("w:id"), "1")
    bm_start.set(_q("w:name"), f"章节{spec.variant + 1}")
    bm_end = etree.Element(_q("w:bookmarkEnd"))
    bm_end.set(_q("w:id"), "1")
    para.insert(0, bm_start)
    link = etree.Element(_q("w:hyperlink"))
    link.set(_q("r:id"), f"rIdLink{spec.variant}")
    _run(link, f"外部链接 {spec.variant + 1}")
    para.append(link)
    para.append(bm_end)
    pkg.set_xml("word/document.xml", root)


def _surgery_equations(pkg: _Package, spec: "Spec") -> None:
    root = pkg.xml("word/document.xml")
    body = _body(root)
    # 行内公式
    para = _paragraph_containing(root, "这是正文小节的一段普通文本")
    _run(para, "等式：")
    omath = etree.Element(_q("m:oMath"))
    mr = _el(omath, "m:r")
    _text(_el(mr, "m:t"), "a+b=c")
    para.append(omath)
    # 独立公式段落（含上标）
    eq_p = etree.Element(_q("w:p"))
    para_math = _el(eq_p, "m:oMathPara")
    omath2 = _el(para_math, "m:oMath")
    base = _el(omath2, "m:sSup")
    e = _el(base, "m:e")
    _text(_el(_el(e, "m:r"), "m:t"), "x")
    sup = _el(base, "m:sup")
    _text(_el(_el(sup, "m:r"), "m:t"), "2")
    body.append(eq_p)
    pkg.set_xml("word/document.xml", root)


def _native_styles(document: Any, spec: "Spec") -> None:
    from docx.enum.style import WD_STYLE_TYPE
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    base = document.styles.add_style(f"定制强调 {spec.slug}", WD_STYLE_TYPE.PARAGRAPH)
    base.base_style = document.styles["Normal"]
    base.font.bold = True
    document.add_paragraph(f"样式继承：本段使用定制样式 {spec.slug}", style=base.name)
    # 主题字体（rFonts asciiTheme/hAnsiTheme）—— python-docx 不能直接设，走底层
    paragraph = document.add_paragraph()
    run = paragraph.add_run("主题字体文本：minorHAnsi / majorHAnsi")
    rpr = run._r.get_or_add_rPr()
    rfonts = OxmlElement("w:rFonts")
    rfonts.set(qn("w:asciiTheme"), "minorHAnsi")
    rfonts.set(qn("w:hAnsiTheme"), "minorHAnsi")
    rfonts.set(qn("w:eastAsiaTheme"), "minorEastAsia")
    rpr.insert(0, rfonts)


def _native_large_document(document: Any, spec: "Spec") -> None:
    for page in range(105):
        document.add_paragraph(
            f"第 {page + 1} 页正文：KPI 汇总 {page + 1}，金额 {page + 1},000.00 元，"
            "本页用于把文档规模推过 100 页。"
        )
        if page != 104:
            document.add_page_break()


# --------------------------------------------------------------------------- #
# 语料计划                                                                     #
# --------------------------------------------------------------------------- #
@dataclass
class Spec:
    """一份语料样本的描述。"""

    slug: str
    category: str
    name: str
    variant: int
    title: str
    notes: str
    build: Callable[["Spec"], bytes] = field(repr=False)


def _make(category: str, name: str, variant: int, title: str, notes: str,
          native: Callable[[Any, Spec], None] | None,
          surgery: Callable[[_Package, Spec], None] | None) -> Spec:
    slug = f"{category}_{variant + 1:02d}"

    def build(spec: Spec) -> bytes:
        return _finalize(spec, native, surgery)

    return Spec(slug=slug, category=category, name=name, variant=variant, title=title,
                notes=notes, build=build)


#: 15 类特征 × (2~3) 份样本 = 41 份（≥40）。
PLAN: list[Spec] = []


def _add(category: str, name: str, titles: list[str], notes: str,
         native: Callable[[Any, Spec], None] | None = None,
         surgery: Callable[[_Package, Spec], None] | None = None) -> None:
    for variant, title in enumerate(titles):
        PLAN.append(_make(category, name, variant, title, notes, native, surgery))


_add("comments", "批注", ["批注样本一", "批注样本二", "批注样本三"],
     "含批注（comments.xml + commentRangeStart/End）", surgery=_surgery_comments)
_add("revisions", "已有修订", ["修订样本一", "修订样本二", "修订样本三"],
     "含已有修订（w:ins / w:del）", surgery=_surgery_revisions)
_add("fields", "域（目录/页码/交叉引用）", ["域样本一", "域样本二", "域样本三"],
     "含域（fldSimple / instrText TOC / REF）", surgery=_surgery_fields)
_add("footnotes", "脚注/尾注", ["脚注样本一", "脚注样本二", "脚注样本三"],
     "含脚注与尾注（footnotes.xml / endnotes.xml）", surgery=_surgery_notes)
_add("merged_cells", "合并单元格", ["合并单元格一", "合并单元格二", "合并单元格三"],
     "含横向 + 纵向合并单元格", native=_native_merged_cells)
_add("nested_tables", "嵌套表格", ["嵌套表格一", "嵌套表格二"],
     "含嵌套表格", native=_native_nested_tables)
_add("textboxes", "文本框/形状", ["文本框一", "文本框二", "文本框三"],
     "含文本框（w:txbxContent）", surgery=_surgery_textboxes)
_add("floating_images", "浮动图片", ["浮动图片一", "浮动图片二"],
     "含浮动图片（wp:anchor）", native=_native_floating_image)
_add("headers_footers", "页眉页脚 + 分节", ["页眉页脚一", "页眉页脚二", "页眉页脚三"],
     "含页眉页脚与多分节", native=_native_headers_footers)
_add("numbering", "多级编号列表", ["多级编号一", "多级编号二", "多级编号三"],
     "含多级编号列表", native=_native_numbering)
_add("content_controls", "内容控件", ["内容控件一", "内容控件二"],
     "含内容控件（w:sdt）", surgery=_surgery_content_controls)
_add("hyperlinks", "超链接/书签", ["超链接书签一", "超链接书签二", "超链接书签三"],
     "含超链接与书签", surgery=_surgery_hyperlinks)
_add("equations", "公式（OMML）", ["公式一", "公式二", "公式三"],
     "含 OMML 公式（m:oMath）", surgery=_surgery_equations)
_add("styles", "样式继承/主题字体", ["样式主题一", "样式主题二", "样式主题三"],
     "含定制样式与主题字体", native=_native_styles)
_add("large_document", "大文档（≥100 页）", ["大文档一", "大文档二"],
     "≥100 页长文档", native=_native_large_document)

#: 浮动图片额外把 wp:inline 转成 wp:anchor
_FLOAT_CATEGORIES = {"floating_images"}


def build_sample(spec: Spec) -> bytes:
    """构建一份语料（含浮动图片的 inline→anchor 后处理）。"""
    data = spec.build(spec)
    if spec.category in _FLOAT_CATEGORIES:
        pkg = _Package(data)
        _float_the_drawing(pkg)
        _normalize_core(pkg)
        data = pkg.to_bytes()
    return data


# --------------------------------------------------------------------------- #
# 目录 / 清单                                                                  #
# --------------------------------------------------------------------------- #
REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = REPO_ROOT / "tests" / "fixtures" / "docx_corpus"
MANIFEST_PATH = REPO_ROOT / "tests" / "corpus" / "manifest.yaml"

#: 每类特征的最少样本数（防空覆盖门禁）。
MIN_SAMPLES = 2

#: TABLE 25 要求的 15 类特征（顺序与计划一致）。
EXPECTED_FEATURES: tuple[str, ...] = tuple(dict.fromkeys(spec.category for spec in PLAN))


class CorpusGateError(ValueError):
    """特征矩阵不满足覆盖门禁（某特征无样本 / 缺特征）时抛出。"""


def assert_full_coverage(
    manifest: dict[str, Any], expected_features: tuple[str, ...] | list[str] | None = None
) -> None:
    """覆盖门禁：每个特征都至少有 ``min_samples`` 份样本，且不缺失特征。

    这是「空覆盖」的显式防线：只要某特征样本数不足，或整个特征在矩阵里缺席，
    就抛 :class:`CorpusGateError` —— 绝不把「无样本」折算成「通过」。
    """
    expected = tuple(expected_features or EXPECTED_FEATURES)
    features = manifest.get("features") or []
    present = [feature.get("id") for feature in features]
    missing = [feature for feature in expected if feature not in present]
    if missing:
        raise CorpusGateError(f"特征矩阵缺少特征：{missing}（空覆盖）")
    for feature in features:
        count = len(feature.get("samples") or [])
        minimum = int(feature.get("min_samples", MIN_SAMPLES))
        if count < minimum:
            raise CorpusGateError(
                f"特征 {feature.get('id')!r} 样本数 {count} < 门禁 {minimum}（空覆盖）"
            )


def load_manifest(path: Path | None = None) -> dict[str, Any]:
    """读取特征矩阵（默认 ``tests/corpus/manifest.yaml``）。"""
    import yaml

    return yaml.safe_load((path or MANIFEST_PATH).read_text(encoding="utf-8"))


def _write_manifest(samples: list[tuple[Spec, bytes]]) -> None:
    import yaml

    by_category: dict[str, list[tuple[Spec, bytes]]] = {}
    for spec, blob in samples:
        by_category.setdefault(spec.category, []).append((spec, blob))

    features: list[dict[str, Any]] = []
    for category in [s.category for s in PLAN]:
        if category in [f["id"] for f in features]:
            continue
        entries = by_category.get(category, [])
        first = entries[0][0]
        features.append(
            {
                "id": category,
                "name": first.name,
                "min_samples": MIN_SAMPLES,
                "technique": first.notes,
                "samples": [
                    {
                        "file": f"{spec.slug}.docx",
                        "sha256": hashlib.sha256(blob).hexdigest(),
                        "marker": _marker(spec.slug),
                        "replacement": _replacement(spec.slug),
                        "notes": spec.notes,
                    }
                    for spec, blob in entries
                ],
            }
        )

    document = {
        "version": 1,
        "generated_by": "tests/corpus/build_corpus.py",
        "corpus_root": "tests/fixtures/docx_corpus",
        "feature_count": len(features),
        "sample_count": len(samples),
        "min_samples_per_feature": MIN_SAMPLES,
        "features": features,
    }
    MANIFEST_PATH.write_text(
        "# T26 保真语料库 · 特征矩阵（全部为合成样本，不含真实客户数据）\n"
        + yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def build_all() -> list[tuple[Spec, bytes]]:
    """构建全部语料、写盘并生成 manifest。"""
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    samples: list[tuple[Spec, bytes]] = []
    for spec in PLAN:
        blob = build_sample(spec)
        (CORPUS_DIR / f"{spec.slug}.docx").write_bytes(blob)
        samples.append((spec, blob))
    _write_manifest(samples)

    readme = (
        "# T26 保真语料库（docx_corpus）\n\n"
        "本目录下所有 `.docx` 均由 `tests/corpus/build_corpus.py` **合成**（python-docx +\n"
        "直接 OOXML 手术），**不含任何真实客户数据**（红线 16）。特征矩阵见\n"
        "`../corpus/manifest.yaml`。请勿手工编辑；`python tests/corpus/build_corpus.py` 可复现。\n"
    )
    (CORPUS_DIR / "README.md").write_text(readme, encoding="utf-8")
    return samples


def verify() -> list[str]:
    """打开每份语料，报告结构计数（自检可读性）。"""
    from docx import Document

    report: list[str] = []
    for spec in PLAN:
        path = CORPUS_DIR / f"{spec.slug}.docx"
        if not path.exists():
            report.append(f"{spec.slug}: MISSING")
            continue
        data = path.read_bytes()
        try:
            doc = Document(io.BytesIO(data))
            report.append(
                f"{spec.slug}: ok paragraphs={len(doc.paragraphs)} "
                f"tables={len(doc.tables)} bytes={len(data)}"
            )
        except Exception as exc:  # noqa: BLE001
            report.append(f"{spec.slug}: OPEN-FAILED {exc!r}")
    return report


def main(argv: list[str]) -> int:
    samples = build_all()
    lines = [
        f"built {len(samples)} samples into {CORPUS_DIR}",
        f"manifest -> {MANIFEST_PATH}",
    ]
    if "--verify" in argv:
        lines.extend(verify())
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
