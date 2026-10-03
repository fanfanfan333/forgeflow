"""INC44 §2.2 — the shared, pure text-diff engine (docx / pptx / textfile).

Before INC44 each document format carried its own paragraph-alignment code. This
module is the **one** implementation of the LCS alignment + the four recomputable
figures, so 「修改 N 处 / 新增 X / 删除 Y / 数字变化 Z」 is defined exactly once
and can never drift between formats.

Layering contract (design §8 — hard, not negotiable):

  * this module is a **pure function** of its inputs — it reads two sequences
    and a numeric-token extractor, and never touches bytes, the filesystem, a
    model or the network;
  * every figure is **recomputable** from the two aligned sequences alone, so a
    diff is unit-testable and can never be a fabrication.

The alignment mirrors ``docx_edit``'s historical behaviour byte-for-byte (an
in-place rewrite of a paragraph is a ``pair`` = a *modification*, not an
add + remove pair) — that is why refactoring ``docx_edit.compute_diff`` onto
this module is a zero-behaviour-change move, pinned by the existing
``tests/unit/test_inc43_docx_edit.py::test_compute_diff_*`` nails.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = ["DiffReport", "align_lines", "diff_counts"]


@dataclass
class DiffReport:
    """Recomputable change counts between two line/paragraph sequences."""

    modified: int = 0
    added: int = 0
    removed: int = 0
    numeric_changes: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "modified": self.modified,
            "added": self.added,
            "removed": self.removed,
            "numeric_changes": self.numeric_changes,
        }


def _lcs_table(old: Sequence[str], new: Sequence[str]) -> list[list[int]]:
    """Classic LCS length table (``dp[i][j]`` = LCS of ``old[i:]`` / ``new[j:]``)."""
    n, m = len(old), len(new)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        row, next_row = dp[i], dp[i + 1]
        for j in range(m - 1, -1, -1):
            if old[i] == new[j]:
                row[j] = next_row[j + 1] + 1
            else:
                row[j] = next_row[j] if next_row[j] >= row[j + 1] else row[j + 1]
    return dp


def align_lines(old: Sequence[str], new: Sequence[str]) -> list[tuple[str, int, int]]:
    """Align two sequences into ``(kind, old_index, new_index)`` triples.

    ``kind`` ∈ ``equal`` | ``pair`` (positional modification) | ``remove`` |
    ``add``. Unmatched runs between two matches are paired **positionally** so an
    in-place rewrite reports as a ``pair`` (a modification), not as an add +
    remove pair. Deterministic and side-effect free.
    """
    dp = _lcs_table(old, new)
    out: list[tuple[str, int, int]] = []

    def _flush(o_start: int, o_end: int, n_start: int, n_end: int) -> None:
        common = min(o_end - o_start, n_end - n_start)
        for k in range(common):
            out.append(("pair", o_start + k, n_start + k))
        for k in range(common, o_end - o_start):
            out.append(("remove", o_start + k, -1))
        for k in range(common, n_end - n_start):
            out.append(("add", -1, n_start + k))

    i = j = 0
    o_run, n_run = 0, 0
    while i < len(old) and j < len(new):
        if old[i] == new[j]:
            _flush(o_run, i, n_run, j)
            out.append(("equal", i, j))
            i += 1
            j += 1
            o_run, n_run = i, j
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    _flush(o_run, len(old), n_run, len(new))
    return out


def diff_counts(
    old: Sequence[str],
    new: Sequence[str],
    numeric_of: Callable[[str], list[str]],
) -> DiffReport:
    """The four recomputable figures between two aligned sequences.

    * ``modified`` — a position that keeps an aligned counterpart but whose text
      changed;
    * ``added`` / ``removed`` — a line with no counterpart;
    * ``numeric_changes`` — of the ``modified`` lines, how many have a different
      **numeric token set** (the 「数字变化 Z」 figure).

    ``numeric_of`` extracts a line's numeric tokens (one shared rule across all
    document formats). Pure and deterministic.
    """
    report = DiffReport()
    for kind, oi, ni in align_lines(old, new):
        if kind == "equal":
            continue
        if kind == "pair":
            report.modified += 1
            if set(numeric_of(old[oi])) != set(numeric_of(new[ni])):
                report.numeric_changes += 1
        elif kind == "add":
            report.added += 1
        elif kind == "remove":
            report.removed += 1
    return report
