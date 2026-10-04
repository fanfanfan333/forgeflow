"""INC46 T22 — 段落级 / run 级 / 表格到单元格级的 diff（纯函数）。

Why this module exists
----------------------
T22 要求「文档编辑先产出 ``pending`` 版本 + diff，用户确认后才提交 ``committed``
版本」。要让人真的看得懂「改了什么」，diff 必须细到：

* **段落级** —— 哪一段被改 / 新增 / 删除；
* **run 级** —— 被改的段落里，哪些 run 变了（增 / 删 / 改），并带上该 run 的
  字符格式（粗体 / 斜体 / 下划线 / 字号 / 颜色），这样「只改了正文、格式没动」
  可以被逐条核对；
* **表格到单元格级** —— 表格结构（行 × 列）与逐个单元格文本的变化。

设计约束（沿用全仓「一个纯函数」纪律）
--------------------------------------
* 本模块是**纯函数**：只读两段字节（或两个已解析的 ``Document``），绝不写盘、绝不
  调模型、绝不联网。任何 figure 都能由两段文档复算出来。
* 段落对齐复用了 :mod:`forgeflow.documents.textdiff` 的**唯一** LCS 引擎
  （``align_lines`` / ``diff_counts``），因此「同一段被改写」报告为 ``modified``
  （``pair``）而不是 add + remove。
* **区间外变化**（T22 的「标红并阻断 approve」）是本模块的
  :meth:`DocumentDiff.out_of_region`：把改动映射回**原文档**的段落下标空间，再与
  目标区间 ``[start, end)`` 比较。无法测量（region 为 ``None``）时返回 ``None``
  （未测量 ⇒ ``None``，红线 4），**绝不**返回空列表冒充「干净」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from forgeflow.documents.docx_inspect import numbers_in_text, open_docx
from forgeflow.documents.textdiff import align_lines, diff_counts

__all__ = [
    "RUN_KIND_CHANGED",
    "RUN_KIND_ADDED",
    "RUN_KIND_REMOVED",
    "PARA_KIND_MODIFIED",
    "PARA_KIND_ADDED",
    "PARA_KIND_REMOVED",
    "CELL_KIND_MODIFIED",
    "CELL_KIND_ADDED",
    "CELL_KIND_REMOVED",
    "RunChange",
    "ParagraphChange",
    "CellChange",
    "TableChange",
    "DocumentDiff",
    "diff_documents",
]

# run 级变化类别。
RUN_KIND_CHANGED = "changed"
RUN_KIND_ADDED = "added"
RUN_KIND_REMOVED = "removed"

# 段落级变化类别。
PARA_KIND_MODIFIED = "modified"
PARA_KIND_ADDED = "added"
PARA_KIND_REMOVED = "removed"

# 单元格级变化类别。
CELL_KIND_MODIFIED = "modified"
CELL_KIND_ADDED = "added"
CELL_KIND_REMOVED = "removed"

#: ``align_lines`` 的 kind → 变化类别（``equal`` 不在其中）。
_RUN_KIND_BY_ALIGN = {
    "pair": RUN_KIND_CHANGED,
    "add": RUN_KIND_ADDED,
    "remove": RUN_KIND_REMOVED,
}
_PARA_KIND_BY_ALIGN = {
    "pair": PARA_KIND_MODIFIED,
    "add": PARA_KIND_ADDED,
    "remove": PARA_KIND_REMOVED,
}


# --------------------------------------------------------------------------- #
# Data shapes                                                                  #
# --------------------------------------------------------------------------- #
@dataclass
class RunChange:
    """一个 run 的变化（含该 run 的字符格式副本，便于逐条核对“格式未动”）。"""

    index: int
    kind: str
    old_text: str
    new_text: str
    old_format: dict[str, Any] = field(default_factory=dict)
    new_format: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "kind": self.kind,
            "old_text": self.old_text,
            "new_text": self.new_text,
            "old_format": dict(self.old_format),
            "new_format": dict(self.new_format),
        }


@dataclass
class ParagraphChange:
    """一个段落的变化（``old_index`` / ``new_index`` 之一可为 ``None``）。"""

    kind: str
    old_index: int | None
    new_index: int | None
    old_text: str
    new_text: str
    runs: list[RunChange] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "old_index": self.old_index,
            "new_index": self.new_index,
            "old_text": self.old_text,
            "new_text": self.new_text,
            "runs": [r.to_dict() for r in self.runs],
        }


@dataclass
class CellChange:
    """一个表格单元格的变化。"""

    row: int
    col: int
    kind: str
    old_text: str
    new_text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "row": self.row,
            "col": self.col,
            "kind": self.kind,
            "old_text": self.old_text,
            "new_text": self.new_text,
        }


@dataclass
class TableChange:
    """一个表格的变化（表级 kind + 逐单元格变化）。"""

    index: int
    kind: str
    old_rows: int | None
    old_cols: int | None
    new_rows: int | None
    new_cols: int | None
    cells: list[CellChange] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "kind": self.kind,
            "old_rows": self.old_rows,
            "old_cols": self.old_cols,
            "new_rows": self.new_rows,
            "new_cols": self.new_cols,
            "cells": [c.to_dict() for c in self.cells],
        }


@dataclass
class DocumentDiff:
    """两段文档之间的完整 diff（可序列化、可复算）。"""

    format: str = "docx"
    paragraphs: list[ParagraphChange] = field(default_factory=list)
    tables: list[TableChange] = field(default_factory=list)
    modified: int = 0
    added: int = 0
    removed: int = 0
    numeric_changes: int = 0

    @property
    def table_cell_changes(self) -> int:
        """所有表格里变化的单元格总数。"""
        return sum(len(t.cells) for t in self.tables)

    def is_clean(self) -> bool:
        """是否有任何变化（段落或表格）。"""
        return bool(self.paragraphs or self.tables)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "paragraphs": [p.to_dict() for p in self.paragraphs],
            "tables": [t.to_dict() for t in self.tables],
            "counts": {
                "modified": self.modified,
                "added": self.added,
                "removed": self.removed,
                "numeric_changes": self.numeric_changes,
                "table_cells": self.table_cell_changes,
            },
        }

    # --- 区间外变化（T22 的阻断依据） ------------------------------------- #
    def out_of_region(self, start: int | None, end: int | None) -> list[dict[str, Any]] | None:
        """目标区间 ``[start, end)`` **之外**的段落变化清单。

        Args:
            start: 目标区间起始段落下标（原文档空间，含）。
            end: 目标区间结束段落下标（原文档空间，不含）。

        Returns:
            * ``None`` —— 区间未测量（``start`` / ``end`` 任一为 ``None``）：诚实
              表示「无法判定是否越界」，**绝不**冒充「无越界」（红线 4）。
            * 列表 —— 越界变化逐条列出（可能为空 = 已测量且干净）。

        映射规则（把改动映射回**原文档**段落下标空间）：

        * ``modified`` / ``removed`` —— 用它自己的 ``old_index``；
        * ``added`` —— 用其**前一个已对齐元素**的原文档下标 ``anchor``（没有则
          ``-1``）；插入发生在 ``anchor + 1`` 处，落在 ``[start, end]`` 即视为区间内。
        """
        if start is None or end is None:
            return None
        out: list[dict[str, Any]] = []
        for change in self.paragraphs:
            if change.kind == PARA_KIND_ADDED:
                anchor = change.old_index if change.old_index is not None else -1
                in_region = start <= (anchor + 1) <= end
                position = anchor + 1
            else:
                assert change.old_index is not None  # modified/removed always carry it
                position = change.old_index
                in_region = start <= position < end
            if not in_region:
                out.append({
                    "kind": change.kind,
                    "old_index": change.old_index,
                    "new_index": change.new_index,
                    "position": position,
                    "old_text": change.old_text,
                    "new_text": change.new_text,
                })
        return out


# --------------------------------------------------------------------------- #
# Run-level diff                                                              #
# --------------------------------------------------------------------------- #
def _run_format(run: Any) -> dict[str, Any]:
    """A small, JSON-safe copy of a run's character formatting."""
    fmt: dict[str, Any] = {}
    bold = getattr(run, "bold", None)
    italic = getattr(run, "italic", None)
    underline = getattr(run, "underline", None)
    if bold is not None:
        fmt["bold"] = bool(bold)
    if italic is not None:
        fmt["italic"] = bool(italic)
    if underline is not None:
        # ``underline`` may be ``True`` or a style enum; stringify the latter.
        fmt["underline"] = underline if isinstance(underline, bool) else str(underline)
    font = getattr(run, "font", None)
    if font is not None:
        if getattr(font, "name", None):
            fmt["font_name"] = str(font.name)
        size = getattr(font, "size", None)
        if size is not None:
            fmt["size_pt"] = size.pt if hasattr(size, "pt") else None
    return fmt


def _diff_runs(old_para: Any, new_para: Any) -> list[RunChange]:
    """Run-level changes for a modified paragraph (with per-run formatting)."""
    old_runs = list(getattr(old_para, "runs", []) or [])
    new_runs = list(getattr(new_para, "runs", []) or [])
    old_texts = [str(getattr(r, "text", "") or "") for r in old_runs]
    new_texts = [str(getattr(r, "text", "") or "") for r in new_runs]
    out: list[RunChange] = []
    for kind, oi, ni in align_lines(old_texts, new_texts):
        if kind == "equal":
            continue
        out.append(
            RunChange(
                index=ni if ni >= 0 else oi,
                kind=_RUN_KIND_BY_ALIGN[kind],
                old_text=old_texts[oi] if oi >= 0 else "",
                new_text=new_texts[ni] if ni >= 0 else "",
                old_format=_run_format(old_runs[oi]) if oi >= 0 else {},
                new_format=_run_format(new_runs[ni]) if ni >= 0 else {},
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Paragraph + table diffs                                                     #
# --------------------------------------------------------------------------- #
def _diff_paragraphs(old_doc: Any, new_doc: Any) -> tuple[list[ParagraphChange], Any]:
    """Paragraph-level changes + the raw alignment (for add-anchor bookkeeping)."""
    old_paras = list(old_doc.paragraphs)
    new_paras = list(new_doc.paragraphs)
    old_texts = [p.text for p in old_paras]
    new_texts = [p.text for p in new_paras]
    changes: list[ParagraphChange] = []

    # ``align_lines`` pairs positionally; walk it and remember the last aligned
    # old index so an inserted paragraph can be anchored back onto the source doc.
    last_old = -1
    for kind, oi, ni in align_lines(old_texts, new_texts):
        if kind == "equal":
            last_old = oi
            continue
        if kind == "pair":
            changes.append(
                ParagraphChange(
                    kind=PARA_KIND_MODIFIED,
                    old_index=oi,
                    new_index=ni,
                    old_text=old_texts[oi],
                    new_text=new_texts[ni],
                    runs=_diff_runs(old_paras[oi], new_paras[ni]),
                )
            )
            last_old = oi
        elif kind == "remove":
            changes.append(
                ParagraphChange(
                    kind=PARA_KIND_REMOVED,
                    old_index=oi,
                    new_index=None,
                    old_text=old_texts[oi],
                    new_text="",
                )
            )
        else:  # add
            changes.append(
                ParagraphChange(
                    kind=PARA_KIND_ADDED,
                    old_index=last_old,
                    new_index=ni,
                    old_text="",
                    new_text=new_texts[ni],
                )
            )
    return changes, (old_texts, new_texts)


def _table_shape(table: Any) -> tuple[int, int]:
    rows = list(table.rows)
    cols = len(rows[0].cells) if rows else 0
    return len(rows), cols


def _cell_text(table: Any, r: int, c: int) -> str:
    try:
        return str(table.rows[r].cells[c].text or "")
    except (IndexError, AttributeError):  # pragma: no cover — ragged table
        return ""


def _diff_tables(old_doc: Any, new_doc: Any) -> list[TableChange]:
    """Table → cell level changes, table-position aligned (deterministic)."""
    old_tables = list(old_doc.tables)
    new_tables = list(new_doc.tables)
    changes: list[TableChange] = []
    for index in range(max(len(old_tables), len(new_tables))):
        old_table = old_tables[index] if index < len(old_tables) else None
        new_table = new_tables[index] if index < len(new_tables) else None
        if old_table is None:
            rows, cols = _table_shape(new_table)
            cells = [CellChange(r, c, CELL_KIND_ADDED, "", _cell_text(new_table, r, c))
                     for r in range(rows) for c in range(cols)]
            changes.append(TableChange(index, CELL_KIND_ADDED, None, None, rows, cols, cells))
            continue
        if new_table is None:
            rows, cols = _table_shape(old_table)
            cells = [CellChange(r, c, CELL_KIND_REMOVED, _cell_text(old_table, r, c), "")
                     for r in range(rows) for c in range(cols)]
            changes.append(TableChange(index, CELL_KIND_REMOVED, rows, cols, None, None, cells))
            continue
        old_rows, old_cols = _table_shape(old_table)
        new_rows, new_cols = _table_shape(new_table)
        cells = []
        for r in range(max(old_rows, new_rows)):
            for c in range(max(old_cols, new_cols)):
                old_text = _cell_text(old_table, r, c) if r < old_rows and c < old_cols else ""
                new_text = _cell_text(new_table, r, c) if r < new_rows and c < new_cols else ""
                if old_text == new_text:
                    continue
                if not old_text:
                    kind = CELL_KIND_ADDED
                elif not new_text:
                    kind = CELL_KIND_REMOVED
                else:
                    kind = CELL_KIND_MODIFIED
                cells.append(CellChange(r, c, kind, old_text, new_text))
        shape_changed = (old_rows, old_cols) != (new_rows, new_cols)
        if cells or shape_changed:
            changes.append(
                TableChange(
                    index=index,
                    kind=CELL_KIND_MODIFIED,
                    old_rows=old_rows,
                    old_cols=old_cols,
                    new_rows=new_rows,
                    new_cols=new_cols,
                    cells=cells,
                )
            )
    return changes


# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #
def diff_documents(old: bytes, new: bytes, *, fmt: str = "docx") -> DocumentDiff:
    """Diff two DOCX documents at paragraph / run / table-cell granularity.

    Args:
        old: the source document bytes.
        new: the edited document bytes.
        fmt: the artifact format label carried on the report (default ``docx``).

    Returns:
        A :class:`DocumentDiff` — pure, recomputable, JSON-serialisable.

    Raises:
        DocxInspectionError: ``old`` / ``new`` is not a readable DOCX.
    """
    old_doc = open_docx(old)
    new_doc = open_docx(new)
    paragraphs, (old_texts, new_texts) = _diff_paragraphs(old_doc, new_doc)
    tables = _diff_tables(old_doc, new_doc)
    counts = diff_counts(old_texts, new_texts, numbers_in_text)

    return DocumentDiff(
        format=fmt,
        paragraphs=paragraphs,
        tables=tables,
        modified=counts.modified,
        added=counts.added,
        removed=counts.removed,
        numeric_changes=counts.numeric_changes,
    )
