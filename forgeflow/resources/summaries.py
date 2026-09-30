"""Resource content summarisation — **pure functions** (INC25 W1).

Everything here is a pure function of ``bytes`` (plus the file name / an
explicit primary-key column): no settings, storage or network. That is what makes
the acceptance criteria mechanically checkable — "for this fixture CSV, rows /
columns / fields are item-by-item equal", "chars == len(text)", "pages ==
extract_pdf_text(...).page_count".

Dependency posture (design §9): the table and ZIP/Excel paths use the **standard
library** (``csv`` / ``zipfile`` / ``xml``). ``pypdf`` (via the existing
``forgeflow.multimodal.pdf`` module) is the one optional extra; when it is absent
the summary degrades to ``metadata_only`` with a verbatim reason instead of
raising — the request still returns HTTP < 500 (AC-8).

Honesty rules:
  * a numeric fact is ``None`` when it was not measured — never a fabricated 0;
  * ``keywords`` is produced ONLY from a real source. This module has no keyword
    extractor, so it never sets ``keywords`` (AC-5: "无真实关键词来源时不产生
    关键词字段");
  * a data-quality conclusion (empty column ratio / duplicate primary key) comes
    from a real statistic over the parsed rows, or is reported as "未指定主键"
    (``duplicate_primary_key=None``) — never invented.
"""

from __future__ import annotations

import csv
import io
import zipfile
from typing import Any
from xml.etree import ElementTree

from forgeflow.resources.models import ResourceSummary

__all__ = [
    "TABLE_EXTENSIONS",
    "EXCEL_EXTENSIONS",
    "PDF_EXTENSIONS",
    "TEXT_EXTENSIONS",
    "IMAGE_EXTENSIONS",
    "SUPPORTED_FILE_EXTENSIONS",
    "content_kind",
    "is_supported_file",
    "summarize_table",
    "summarize_text",
    "summarize_pdf",
    "summarize_excel",
    "summarize_bytes",
]

TABLE_EXTENSIONS: tuple[str, ...] = (".csv", ".tsv", ".tab")
EXCEL_EXTENSIONS: tuple[str, ...] = (".xlsx", ".xlsm")
PDF_EXTENSIONS: tuple[str, ...] = (".pdf",)
TEXT_EXTENSIONS: tuple[str, ...] = (
    ".txt", ".md", ".markdown", ".rst", ".json", ".jsonl", ".ndjson", ".yaml",
    ".yml", ".log", ".ini", ".cfg", ".toml", ".xml", ".html", ".htm", ".py",
    ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".c", ".h", ".cpp",
    ".rb", ".php", ".sh", ".sql", ".css", ".env",
)
IMAGE_EXTENSIONS: tuple[str, ...] = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff",
)

SUPPORTED_FILE_EXTENSIONS: tuple[str, ...] = (
    TABLE_EXTENSIONS + EXCEL_EXTENSIONS + PDF_EXTENSIONS + TEXT_EXTENSIONS + IMAGE_EXTENSIONS
)

_MIME_BY_EXTENSION: dict[str, str] = {
    ".csv": "text/csv", ".tsv": "text/tab-separated-values", ".tab": "text/tab-separated-values",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/markdown",
    ".json": "application/json", ".yaml": "application/x-yaml", ".yml": "application/x-yaml",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff",
}


def _suffix(name: str) -> str:
    lowered = (name or "").strip().lower()
    dot = lowered.rfind(".")
    return lowered[dot:] if dot >= 0 else ""


def mime_for(name: str) -> str:
    """Best-effort MIME by extension (empty string when unknown)."""
    return _MIME_BY_EXTENSION.get(_suffix(name), "")


def content_kind(name: str) -> str:
    """Classify a file by extension: table | excel | pdf | text | image | unsupported."""
    ext = _suffix(name)
    if ext in TABLE_EXTENSIONS:
        return "table"
    if ext in EXCEL_EXTENSIONS:
        return "excel"
    if ext in PDF_EXTENSIONS:
        return "pdf"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in TEXT_EXTENSIONS:
        return "text"
    return "unsupported"


def is_supported_file(name: str) -> bool:
    """True when a file of this name has a resource-summary path."""
    return content_kind(name) != "unsupported"


def _decode(data: bytes) -> tuple[str, str]:
    """Decode text bytes, returning ``(text, encoding_used)``.

    Tries UTF-8 (with BOM) strictly, then GB18030 (common in CJK CSVs), then
    falls back to Latin-1 with replacement so a decode can never raise. The
    encoding used is reported so a lossy decode is visible, never hidden.
    """
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    try:
        return data.decode("gb18030"), "gb18030"
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace"), "latin-1(replace)"


def _sniff_delimiter(sample: str, filename: str) -> str:
    """Pick a delimiter: extension first, then the most frequent in the header."""
    ext = _suffix(filename)
    if ext in (".tsv", ".tab"):
        return "\t"
    header = sample.splitlines()[0] if sample.splitlines() else ""
    counts = {",": header.count(","), "\t": header.count("\t"), ";": header.count(";")}
    best = max(counts, key=lambda k: counts[k])
    return best if counts[best] > 0 else ","


def _pick_primary_key(header: list[str], explicit: str | None) -> str | None:
    """The primary-key column: explicit wins, else a header column literally ``id``.

    We do **not** guess. With no explicit key and no column named ``id`` this
    returns ``None`` and the caller reports "未指定主键" rather than inventing one
    (design §11 — AC-4).
    """
    if explicit and explicit in header:
        return explicit
    for col in header:
        if col.strip().lower() == "id":
            return col
    return None


def _quality(header: list[str], rows: list[list[str]], primary_key: str | None) -> dict[str, Any]:
    """Real data-quality statistics: per-column empty ratio + duplicate primary key."""
    n = len(rows)
    empty_counts: dict[str, int] = {}
    for idx, col in enumerate(header):
        count = sum(1 for row in rows if idx >= len(row) or str(row[idx]).strip() == "")
        if count > 0:
            empty_counts[col] = count
    empty_columns = {
        col: (count / n if n else 0.0) for col, count in empty_counts.items()
    }

    duplicate_primary_key: bool | None = None
    resolved_key: str | None = None
    if primary_key and primary_key in header:
        resolved_key = primary_key
        key_idx = header.index(primary_key)
        values = [str(row[key_idx]).strip() for row in rows if key_idx < len(row)]
        duplicate_primary_key = len(values) != len(set(values))

    return {
        "rows": n,
        "empty_columns": empty_columns,
        "empty_counts": empty_counts,
        "primary_key": resolved_key,
        "duplicate_primary_key": duplicate_primary_key,
    }


def summarize_table(
    data: bytes,
    *,
    filename: str = "",
    primary_key: str | None = None,
) -> ResourceSummary:
    """Summarise a delimited-text table (CSV/TSV) from raw bytes.

    Returns a :class:`ResourceSummary` whose ``rows`` / ``columns`` / ``fields``
    are item-by-item equal to the file's real content, and whose ``quality``
    carries the real empty-column ratios and the duplicate-primary-key verdict.

    ``rows`` counts **data** rows (the header is not a data row). An empty file
    yields ``rows=0``, ``columns=0``, ``fields=[]``.
    """
    text, encoding = _decode(data)
    delimiter = _sniff_delimiter(text, filename)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    parsed = list(reader)
    if not parsed or (len(parsed) == 1 and not any(cell.strip() for cell in parsed[0])):
        return ResourceSummary(
            kind="file", rows=0, columns=0, fields=[],
            quality={"rows": 0, "empty_columns": {}, "empty_counts": {},
                     "primary_key": None, "duplicate_primary_key": None,
                     "encoding": encoding},
            note="空表格",
        )
    header = [str(c).strip() for c in parsed[0]]
    body = [row for row in parsed[1:] if any(str(cell).strip() for cell in row)]
    key = _pick_primary_key(header, primary_key)
    quality = _quality(header, body, key)
    quality["encoding"] = encoding
    return ResourceSummary(
        kind="file",
        rows=len(body),
        columns=len(header),
        fields=header,
        quality=quality,
        note="" if key else "未指定主键列，未做重复主键判定",
    )


def summarize_text(data: bytes, *, filename: str = "") -> ResourceSummary:
    """Summarise a text file: ``chars == len(text)`` exactly."""
    text, encoding = _decode(data)
    return ResourceSummary(
        kind="file",
        chars=len(text),
        quality={"lines": text.count("\n") + (1 if text and not text.endswith("\n") else 0),
                 "encoding": encoding},
    )


def summarize_pdf(data: bytes) -> tuple[str, ResourceSummary, str]:
    """Summarise a PDF via the existing ``multimodal.pdf`` extractor.

    Returns ``(status, summary, detail)``. When ``pypdf`` is not installed the
    status is ``metadata_only`` with a verbatim reason — the request must not 5xx
    (AC-8). When it parses, ``summary.pages`` equals ``extract_pdf_text(...).page_count``
    and ``summary.chars`` equals ``len(text)``.
    """
    from forgeflow.multimodal.pdf import extract_pdf_text

    try:
        document = extract_pdf_text(data)
    except ImportError as exc:
        return (
            "metadata_only",
            ResourceSummary(kind="file", note="pdf support unavailable"),
            f"pdf support unavailable: {exc}",
        )
    except ValueError as exc:
        return (
            "metadata_only",
            ResourceSummary(kind="file", note="unparseable pdf"),
            f"unparseable pdf: {exc}",
        )
    except Exception as exc:  # noqa: BLE001 — a parse must never break the request
        return (
            "metadata_only",
            ResourceSummary(kind="file", note="pdf parse failed"),
            f"pdf parse failed: {exc}",
        )
    text = document.text or ""
    return (
        "parsed",
        ResourceSummary(kind="file", chars=len(text), pages=document.page_count),
        "",
    )


# --------------------------------------------------------------------------- #
# Excel (.xlsx) — parsed with the standard library (zipfile + xml), no openpyxl #
# --------------------------------------------------------------------------- #
_XLSX_MAIN_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _xml_local(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        raw = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ElementTree.fromstring(raw)
    strings: list[str] = []
    for si in root:
        if _xml_local(si.tag) != "si":
            continue
        strings.append("".join(node.text or "" for node in si.iter() if _xml_local(node.tag) == "t"))
    return strings


def _xlsx_first_sheet(archive: zipfile.ZipFile) -> list[list[str]]:
    name = next(
        (n for n in archive.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")),
        None,
    )
    if name is None:
        return []
    root = ElementTree.fromstring(archive.read(name))
    shared = _xlsx_shared_strings(archive)
    rows: list[list[str]] = []
    for row in root.iter():
        if _xml_local(row.tag) != "row":
            continue
        cells: list[str] = []
        for cell in row:
            if _xml_local(cell.tag) != "c":
                continue
            ctype = cell.attrib.get("t", "")
            value_node = next(
                (c for c in cell if _xml_local(c.tag) in ("v", "is")), None
            )
            value = ""
            if value_node is not None:
                if _xml_local(value_node.tag) == "is":
                    value = "".join(
                        node.text or "" for node in value_node.iter() if _xml_local(node.tag) == "t"
                    )
                else:
                    value = value_node.text or ""
            if ctype == "s" and value.isdigit():
                idx = int(value)
                value = shared[idx] if 0 <= idx < len(shared) else value
            cells.append(value)
        rows.append(cells)
    return rows


def summarize_excel(data: bytes, *, filename: str = "", primary_key: str | None = None) -> ResourceSummary:
    """Summarise an ``.xlsx`` workbook using only the standard library.

    Uses the first worksheet. Raises ``ValueError`` (caught by the dispatcher,
    which degrades to ``metadata_only``) when the archive is not a readable
    workbook — it never fabricates counts.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            rows = _xlsx_first_sheet(archive)
    except (zipfile.BadZipFile, ElementTree.ParseError, KeyError) as exc:
        raise ValueError(f"unreadable xlsx: {exc}") from exc
    if not rows:
        return ResourceSummary(kind="file", rows=0, columns=0, fields=[], note="空工作簿")
    header = [str(c).strip() for c in rows[0]]
    body = [row for row in rows[1:] if any(str(cell).strip() for cell in row)]
    key = _pick_primary_key(header, primary_key)
    quality = _quality(header, body, key)
    return ResourceSummary(
        kind="file", rows=len(body), columns=len(header), fields=header, quality=quality,
        note="" if key else "未指定主键列，未做重复主键判定",
    )


def summarize_bytes(
    data: bytes,
    *,
    filename: str = "",
    primary_key: str | None = None,
) -> tuple[str, ResourceSummary, str]:
    """Dispatch a file resource to its summariser.

    Returns ``(status, summary, detail)``. ``status`` is the resource status to
    persist (``parsed`` / ``metadata_only`` / ``ignored``); ``detail`` is the
    verbatim reason for a degraded / ignored outcome (``""`` on success).
    """
    kind = content_kind(filename)
    if kind == "table":
        return "parsed", summarize_table(data, filename=filename, primary_key=primary_key), ""
    if kind == "text":
        return "parsed", summarize_text(data, filename=filename), ""
    if kind == "pdf":
        return summarize_pdf(data)
    if kind == "excel":
        try:
            return "parsed", summarize_excel(data, filename=filename, primary_key=primary_key), ""
        except ValueError as exc:
            return (
                "metadata_only",
                ResourceSummary(kind="file", note="excel parse failed"),
                str(exc),
            )
    if kind == "image":
        return (
            "metadata_only",
            ResourceSummary(kind="file", note="image content not summarised"),
            "图片资源仅登记元数据；资源摘要不解析图像内容",
        )
    return (
        "ignored",
        ResourceSummary(kind="file"),
        f"不支持的文件类型：{_suffix(filename) or '(无扩展名)'}",
    )
