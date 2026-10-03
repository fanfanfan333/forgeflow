"""INC46 T26 — per-part OOXML package comparison (the fidelity comparator).

What this is
------------
A **per-part** comparison of two DOCX byte strings. Per-part matters: the
central fidelity question is *"did every part survive?"* — a document whose
``word/footnotes.xml`` silently disappeared still opens fine in Word, so a
paragraph-text-only check cannot see the loss. This comparator reports exactly
which parts disappeared, appeared, or changed, with a concrete difference.

Where it lives (decision)
-------------------------
T26 explicitly forbids touching ``forgeflow/`` source, so the comparator lives
beside the corpus under ``tests/corpus/`` instead of ``forgeflow/documents/``.
T23 (five-layer validation stack) is expected to consume/move it — the public
surface (:func:`read_package`, :func:`compare_packages`, :class:`PackageDiff`,
:func:`body_paragraph_signatures`) is stable and dependency-free (stdlib +
``lxml``, already a hard dependency of ``python-docx``).

Comparison rules (explicit, no "fuzzy equality")
------------------------------------------------
1. **Parts are addressed by name.** Zip order, zip timestamps and compression
   are never compared — only decompressed part content.
2. **XML parts are compared canonically** (C14N 2.0) after stripping the
   volatile ``w:rsid*`` attributes Word assigns to every edit session. Two parts
   with the same content but a differently-quoted XML declaration compare equal
   (measured: that is the *only* difference ``python-docx`` introduces on
   ``comments.xml``).
3. **``[Content_Types].xml`` is compared order-insensitively** as a sorted set of
   declared (kind, key, content-type) triples — the OPC spec does not fix child
   order and ``python-docx`` re-emits ``<Override>`` alphabetically (measured).
   Nothing else about it is relaxed.
4. **Whitelisted volatile parts** (:data:`VOLATILE_PART_WHITELIST`) are excluded
   from the *content* comparison and reported separately as ``whitelisted`` —
   the list is explicit and deliberately tiny (document timestamps only).
5. Everything else — ``word/document.xml``, headers/footers, comments,
   footnotes, endnotes, numbering, styles, themes, media blobs — is compared for
   real, byte-for-byte (binary) or canonically (XML).
"""

from __future__ import annotations

import copy
import io
import zipfile
from dataclasses import dataclass, field
from typing import Any

import lxml.etree as etree

__all__ = [
    "VOLATILE_PART_WHITELIST",
    "VOLATILE_ATTRIBUTES",
    "ORDER_INSENSITIVE_PARTS",
    "PackageCompareError",
    "PartDelta",
    "PackageDiff",
    "read_package",
    "compare_packages",
    "body_paragraph_signatures",
    "has_part",
]

#: Whole parts excluded from content comparison. Deliberately explicit and
#: tiny: these two carry document *timestamps / authorship / app statistics*,
#: which no fidelity claim can be made about (and which Word rewrites on open).
VOLATILE_PART_WHITELIST: tuple[str, ...] = (
    "docProps/core.xml",  # dcterms:created / modified / revision / lastModifiedBy
    "docProps/app.xml",  # TotalTime / AppVersion / application statistics
)

#: Attributes Word stamps per edit session; stripped before canonical compare.
#: (``w:`` namespace — the prefix is fixed for OOXML.)
VOLATILE_ATTRIBUTES: tuple[str, ...] = (
    "rsid",
    "rsidR",
    "rsidRPr",
    "rsidDel",
    "rsidP",
    "rsidRDefault",
    "rsidTr",
    "rsidSect",
    "rsidRoot",
)

#: Parts whose *child order* is not significant per the OPC spec (python-docx
#: re-emits them sorted). Compared as a sorted structural triple-set.
ORDER_INSENSITIVE_PARTS: tuple[str, ...] = ("[Content_Types].xml",)

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"


class PackageCompareError(ValueError):
    """Raised when a blob is not a readable ZIP/OOXML package."""


@dataclass
class PartDelta:
    """One part that differs between the two packages."""

    name: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"part": self.name, "detail": self.detail}


@dataclass
class PackageDiff:
    """The full, per-part difference between two packages."""

    only_in_a: list[str] = field(default_factory=list)
    only_in_b: list[str] = field(default_factory=list)
    differing: list[PartDelta] = field(default_factory=list)
    whitelisted: list[str] = field(default_factory=list)
    identical: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """True only when every comparable part is present and identical."""
        return not (self.only_in_a or self.only_in_b or self.differing)

    def summary(self) -> str:
        return (
            f"仅 A 有 {len(self.only_in_a)}：{self.only_in_a}；"
            f"仅 B 有 {len(self.only_in_b)}：{self.only_in_b}；"
            f"内容不同 {len(self.differing)}："
            f"{[d.name for d in self.differing]}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_empty": self.is_empty,
            "only_in_a": list(self.only_in_a),
            "only_in_b": list(self.only_in_b),
            "differing": [d.to_dict() for d in self.differing],
            "whitelisted": list(self.whitelisted),
            "identical": list(self.identical),
        }


# --------------------------------------------------------------------------- #
# reading / normalising                                                        #
# --------------------------------------------------------------------------- #
def read_package(data: bytes) -> dict[str, bytes]:
    """Decompress every part of a DOCX into ``{part_name: bytes}``.

    Raises:
        PackageCompareError: ``data`` is empty or not a readable ZIP package.
    """
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise PackageCompareError("DOCX 字节为空，无法比较部件")
    try:
        with zipfile.ZipFile(io.BytesIO(bytes(data))) as archive:
            return {name: archive.read(name) for name in archive.namelist()}
    except zipfile.BadZipFile as exc:
        raise PackageCompareError(f"不是合法的 DOCX/ZIP 包：{exc}") from exc


def has_part(data: bytes, name: str) -> bool:
    """Whether the package contains ``name`` (no exception for a bad package)."""
    try:
        return name in read_package(data)
    except PackageCompareError:
        return False


def _strip_volatile_attributes(root: etree._Element) -> None:
    """Remove ``w:rsid*`` attributes in place (Word edit-session stamps)."""
    for element in root.iter():
        for attr in list(element.attrib):
            local = etree.QName(attr).localname if "}" in attr else attr
            if local in VOLATILE_ATTRIBUTES:
                del element.attrib[attr]
            elif local.startswith("rsid"):
                del element.attrib[attr]


def _canonical_xml(blob: bytes) -> bytes:
    """Canonical (C14N 2.0) serialisation of an XML part, rsid-stripped."""
    root = etree.fromstring(blob)
    _strip_volatile_attributes(root)
    return etree.tostring(root, method="c14n2")


def _content_type_triples(blob: bytes) -> list[tuple[str, str, str]]:
    """``[Content_Types].xml`` → a sorted (kind, key, content-type) triple set."""
    root = etree.fromstring(blob)
    triples: set[tuple[str, str, str]] = set()
    for child in root:
        kind = etree.QName(child).localname
        key = child.get("PartName") or child.get("Extension") or ""
        triples.add((kind, key, child.get("ContentType") or ""))
    return sorted(triples)


def _is_xml_part(name: str, blob: bytes) -> bool:
    if not (name.endswith(".xml") or name.endswith(".rels")):
        return False
    head = blob.lstrip()[:5]
    return head.startswith(b"<?xml") or head.startswith(b"<")


def _first_byte_difference(a: bytes, b: bytes) -> int | None:
    for index, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return index
    return None


def _xml_difference_detail(a: bytes, b: bytes, name: str) -> str:
    """A short, concrete difference description for one XML part."""
    try:
        ca, cb = _canonical_xml(a), _canonical_xml(b)
    except etree.XMLSyntaxError as exc:
        return f"部件 {name} 无法解析为 XML：{exc}"
    if ca == cb:  # pragma: no cover — canonical equality is checked by the caller
        return f"部件 {name} 规范化后相同"
    import difflib

    lines = list(
        difflib.unified_diff(
            ca.decode("utf-8", "replace").splitlines(),
            cb.decode("utf-8", "replace").splitlines(),
            lineterm="",
            n=0,
        )
    )
    excerpt = " | ".join(lines[2:6]) or "（规范化差异在声明/空白层）"
    size = _first_byte_difference(ca, cb)
    return f"部件 {name} 规范化内容不同（首个差异字节 offset={size}）：{excerpt}"


def _binary_difference_detail(a: bytes, b: bytes, name: str) -> str:
    offset = _first_byte_difference(a, b)
    if offset is None:
        return f"部件 {name} 长度不同：A={len(a)} 字节，B={len(b)} 字节"
    return (
        f"部件 {name} 二进制内容不同：A={len(a)} 字节，B={len(b)} 字节，"
        f"首个差异 offset={offset}"
    )


def compare_packages(a: bytes, b: bytes) -> PackageDiff:
    """Compare two DOCX packages part by part.

    ``a`` is the reference (original), ``b`` the produced document. The result
    lists missing/extra parts, content differences (with a concrete detail per
    part), the whitelisted volatile parts, and the identical ones.
    """
    parts_a = read_package(a)
    parts_b = read_package(b)
    diff = PackageDiff()
    for name in sorted(set(parts_a) - set(parts_b)):
        diff.only_in_a.append(name)
    for name in sorted(set(parts_b) - set(parts_a)):
        diff.only_in_b.append(name)
    for name in sorted(set(parts_a) & set(parts_b)):
        if name in VOLATILE_PART_WHITELIST:
            diff.whitelisted.append(name)
            continue
        blob_a, blob_b = parts_a[name], parts_b[name]
        if blob_a == blob_b:
            diff.identical.append(name)
            continue
        if name in ORDER_INSENSITIVE_PARTS:
            if _content_type_triples(blob_a) == _content_type_triples(blob_b):
                diff.identical.append(name)
                continue
            diff.differing.append(
                PartDelta(
                    name=name,
                    detail=f"部件 {name} 声明的类型/扩展集合不同"
                    f"（A={_content_type_triples(blob_a)}，B={_content_type_triples(blob_b)}）",
                )
            )
            continue
        if _is_xml_part(name, blob_a) and _is_xml_part(name, blob_b):
            if _canonical_xml(blob_a) == _canonical_xml(blob_b):
                diff.identical.append(name)
                continue
            diff.differing.append(
                PartDelta(name=name, detail=_xml_difference_detail(blob_a, blob_b, name))
            )
            continue
        diff.differing.append(
            PartDelta(name=name, detail=_binary_difference_detail(blob_a, blob_b, name))
        )
    return diff


# --------------------------------------------------------------------------- #
# paragraph-level view of document.xml (directed-edit assertions)              #
# --------------------------------------------------------------------------- #
def body_paragraph_signatures(data: bytes) -> list[str]:
    """Canonical XML of every **body-level** ``w:p``, in document order.

    This mirrors ``python-docx``'s ``Document.paragraphs`` view (direct ``w:p``
    children of ``w:body``), so an edit addressed by paragraph index can be
    checked at the very same granularity: which paragraph changed, and — just as
    importantly — which ones did not.
    """
    parts = read_package(data)
    root = etree.fromstring(parts["word/document.xml"])
    body = root.find(f"{{{_W_NS}}}body")
    if body is None:
        raise PackageCompareError("word/document.xml 缺少 w:body")
    signatures: list[str] = []
    for child in body:
        if etree.QName(child).localname == "p":
            # c14n2 needs every namespace of the subtree declared in scope; the
            # declarations live on the ancestor ``w:document``. Serialise a
            # detached copy with the needed declarations pushed onto it.
            node = copy.deepcopy(child)
            _strip_volatile_attributes(node)
            etree.cleanup_namespaces(node)
            signatures.append(etree.tostring(node, method="c14n2").decode("utf-8"))
    return signatures
