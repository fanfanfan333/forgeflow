"""INC46 T20 — deterministic target locating (自然语言目标 → 文档区间).

The locator turns a *target selector* (「第三部分」/「第 3 章」/「三、」/
「第三个一级标题」/「标题含 xxx」) into document-anchored candidates. It is pure and
deterministic: **no LLM, no network, no bytes written** — it only reads a parsed
:class:`~forgeflow.documents.docx_inspect.DocStructure`.

Design (design §「目标定位」)
----------------------------
* Two *numbering namespaces* are consulted, both real:

  1. **text identity** — a heading whose own text begins with a division marker
     (``第三章`` / ``三、`` / ``3.``), parsed by
     :func:`~forgeflow.documents.docx_inspect.text_marker`;
  2. **``numPr`` numbering domain** — a heading's *auto* numbering label
     (``三、``) resolved from ``numbering.xml`` by ``inspect_docx``.

* A located target is returned **only** when it is unique *and* its confidence is
  ``>= CONFIDENCE_THRESHOLD``. Otherwise an ``ambiguity`` list (all candidates) is
  returned and **nothing is auto-selected** (禁止猜测). Zero candidates ⇒ an
  honest ``not_found`` result that also lists the document's existing headings
  (never a fabricated target).

* ``confidence`` is always a *computed, explainable* score (≥ threshold for a
  single-source hit, higher when two independent sources agree). When a score
  cannot be measured it is ``None`` — never ``0`` (red line 4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from forgeflow.documents.docx_inspect import (
    DocStructure,
    chinese_number_to_int,
    inspect_docx,
    text_marker,
)

__all__ = [
    "CONFIDENCE_THRESHOLD",
    "LocatorCandidate",
    "LocatorResult",
    "SelectorSpec",
    "locate",
    "parse_selector",
]

#: Initial default locate-confidence floor (任务书 §十 — must be calibratable, so
#: it lives in exactly one place and every entry point takes an override).
CONFIDENCE_THRESHOLD: float = 0.8

#: Confidence of a candidate backed by exactly one numbering namespace.
_SINGLE_SOURCE_CONFIDENCE: float = 0.85
#: Confidence when a heading is confirmed by an explicit structure query
#: (「第 N 个一级标题」/「标题含 …」) — these are unambiguous by construction.
_STRUCTURAL_CONFIDENCE: float = 0.9
_CONTAINS_CONFIDENCE: float = 0.95
#: Cap applied when two independent sources agree on one heading.
_AGREEMENT_CAP: float = 0.97

#: Division-kind words that are interchangeable for a pure "which division"
#: reference (a user may say 部分 where the document writes 章). Both sides must be
#: in this set for the cross-kind match to fire, so an unrelated kind never matches.
_DIVISION_KINDS: frozenset[str] = frozenset({"部分", "章", "节", "条", "篇", "编"})

# --------------------------------------------------------------------------- #
# Selector grammar                                                             #
# --------------------------------------------------------------------------- #
#: 「标题含 X」/「标题包含 X」/「标题文字带有 X」 … (longer alternatives first).
_CONTAINS_RE = re.compile(
    r"^\s*标题(?:文字)?(?:中|里)?\s*(?:包含|含有|带有|中带|含|带)\s*(.+?)\s*$"
)
#: 「第 N 个 [一级] 标题」 — ordinal + optional outline level.
_INDEXED_RE = re.compile(
    r"^\s*第\s*([0-9一二三四五六七八九十百零两]+)\s*个\s*(?:第)?\s*"
    r"([一二三四五六七八九十]+)?\s*级?\s*标题\s*$"
)
#: 「第 N 部分/章/节/条/篇/编」 (kind optional).
_DIVISION_RE = re.compile(r"^\s*第\s*([0-9一二三四五六七八九十百零两]+)\s*(部分|章|节|条|篇|编)?\s*$")
#: 「N、」/「N.」/「N)」 — a bare enumerated-item marker.
_MARKER_RE = re.compile(r"^\s*([0-9]+|[一二三四五六七八九十百零两]+)\s*[、.．)）]\s*$")


def _parse_ordinal(text: str | None) -> int | None:
    """Arabic integer or Chinese numeral → ``int`` (``None`` when unparsable)."""
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    return chinese_number_to_int(raw)


@dataclass
class SelectorSpec:
    """A parsed target selector."""

    raw: str
    mode: str  # "contains" | "indexed" | "division" | "marker" | "unparsed"
    ordinal: int | None = None
    kind: str | None = None
    level: int | None = None
    contains: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "mode": self.mode,
            "ordinal": self.ordinal,
            "kind": self.kind,
            "level": self.level,
            "contains": self.contains,
        }


def parse_selector(selector: str | None) -> SelectorSpec:
    """Parse a target selector into a :class:`SelectorSpec` (deterministic)."""
    raw = str(selector or "").strip()
    match = _CONTAINS_RE.match(raw)
    if match:
        return SelectorSpec(raw, "contains", contains=match.group(1).strip())
    match = _INDEXED_RE.match(raw)
    if match:
        level = chinese_number_to_int(match.group(2)) if match.group(2) else None
        return SelectorSpec(raw, "indexed", ordinal=_parse_ordinal(match.group(1)), level=level)
    match = _DIVISION_RE.match(raw)
    if match:
        return SelectorSpec(raw, "division", ordinal=_parse_ordinal(match.group(1)), kind=match.group(2))
    match = _MARKER_RE.match(raw)
    if match:
        return SelectorSpec(raw, "marker", ordinal=_parse_ordinal(match.group(1)), kind="、")
    return SelectorSpec(raw, "unparsed")


# --------------------------------------------------------------------------- #
# Result types                                                                 #
# --------------------------------------------------------------------------- #
@dataclass
class LocatorCandidate:
    """One document-anchored candidate for a target selector."""

    index: int
    level: int
    label: str
    confidence: float | None
    evidence: str
    section_end_index: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "level": self.level,
            "label": self.label,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "section_end_index": self.section_end_index,
        }


@dataclass
class LocatorResult:
    """The honest outcome of a locate: chosen / ambiguous / not-found."""

    selector: str
    kind: str
    candidates: list[LocatorCandidate] = field(default_factory=list)
    ambiguity: list[LocatorCandidate] = field(default_factory=list)
    chosen: LocatorCandidate | None = None
    not_found: bool = False
    structure: list[dict[str, Any]] = field(default_factory=list)
    confidence_threshold: float = CONFIDENCE_THRESHOLD
    error: str | None = None

    @property
    def located(self) -> bool:
        """True iff exactly one confident candidate was chosen."""
        return self.chosen is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "selector": self.selector,
            "kind": self.kind,
            "candidates": [c.to_dict() for c in self.candidates],
            "ambiguity": [c.to_dict() for c in self.ambiguity],
            "chosen": self.chosen.to_dict() if self.chosen is not None else None,
            "not_found": self.not_found,
            "located": self.located,
            "structure": list(self.structure),
            "confidence_threshold": self.confidence_threshold,
            "error": self.error,
        }


# --------------------------------------------------------------------------- #
# Matching                                                                     #
# --------------------------------------------------------------------------- #
def _kind_compatible(requested: str | None, candidate: str | None) -> bool:
    """Whether a requested division kind accepts a candidate's kind word.

    A bare enumerated-item marker (``kind == "、"``) is kind-agnostic; two
    different *division* words (``部分`` vs ``章``) are accepted as the same
    structural reference. Anything else must match exactly.
    """
    if requested is None or candidate is None:
        return True
    if candidate == "、":
        return True
    if requested == candidate:
        return True
    return requested in _DIVISION_KINDS and candidate in _DIVISION_KINDS


def _heading_label(heading: dict[str, Any]) -> str:
    """A heading's display label: auto ``numPr`` label, else its text marker."""
    label = heading.get("numbering_label")
    if label:
        return str(label)
    marker = text_marker(heading.get("text")).get("marker")
    return str(marker) if marker else str(heading.get("text") or "")


def _add_candidate(
    collected: dict[int, LocatorCandidate],
    heading: dict[str, Any],
    confidence: float,
    evidence: str,
) -> None:
    """Insert a candidate, merging (and strengthening) a duplicate heading hit."""
    index = int(heading["index"])
    existing = collected.get(index)
    if existing is None:
        collected[index] = LocatorCandidate(
            index=index,
            level=int(heading["level"]),
            label=_heading_label(heading),
            confidence=confidence,
            evidence=evidence,
            section_end_index=int(heading.get("section_end_index", index + 1)),
        )
        return
    # Two independent numbering namespaces agree on this heading ⇒ stronger.
    merged = min(_AGREEMENT_CAP, (existing.confidence or confidence) + 0.1)
    existing.confidence = merged
    if evidence not in existing.evidence:
        existing.evidence = f"{existing.evidence}；{evidence}"


def _heading_candidates(structure: DocStructure, spec: SelectorSpec) -> list[LocatorCandidate]:
    """All headings matching ``spec`` (order = document order)."""
    headings = list(structure.headings)
    collected: dict[int, LocatorCandidate] = {}

    if spec.mode == "contains":
        needle = spec.contains or ""
        if needle:
            for heading in headings:
                if needle in str(heading.get("text") or ""):
                    _add_candidate(collected, heading, _CONTAINS_CONFIDENCE, f"标题文本包含 {needle!r}")
    elif spec.mode == "indexed":
        pool = [h for h in headings if spec.level is None or int(h["level"]) == spec.level]
        ordinal = spec.ordinal or 0
        if 1 <= ordinal <= len(pool):
            heading = pool[ordinal - 1]
            level_txt = f"{spec.level}级" if spec.level else ""
            _add_candidate(
                collected, heading, _STRUCTURAL_CONFIDENCE, f"第 {ordinal} 个{level_txt}标题（文档顺序）"
            )
    elif spec.mode in ("division", "marker"):
        for heading in headings:
            marker = text_marker(heading.get("text"))
            if marker["ordinal"] == spec.ordinal and _kind_compatible(spec.kind, marker["kind"]):
                _add_candidate(
                    collected, heading, _SINGLE_SOURCE_CONFIDENCE, f"正文编号标记 {marker['marker']!r}"
                )
            label = heading.get("numbering_label")
            if label:
                label_marker = text_marker(label)
                if label_marker["ordinal"] == spec.ordinal and _kind_compatible(spec.kind, label_marker["kind"]):
                    _add_candidate(
                        collected, heading, _SINGLE_SOURCE_CONFIDENCE, f"自动编号标签 {label!r}"
                    )

    return [collected[k] for k in sorted(collected)]


def _apply_ambiguity_policy(
    candidates: list[LocatorCandidate], threshold: float
) -> tuple[list[LocatorCandidate], list[LocatorCandidate]]:
    """Decide ``(selected, ambiguity)``.

    A target is chosen **only** when there is exactly one candidate whose
    confidence is measurable and ``>= threshold``. More than one candidate, or a
    best candidate below the floor, is ambiguous — and nothing is auto-selected.
    This is the single injection point the counterfactual test overrides.
    """
    if not candidates:
        return [], []
    if len(candidates) > 1:
        return [], list(candidates)
    best = candidates[0]
    if best.confidence is None or best.confidence < threshold:
        return [], list(candidates)
    return [best], []


def _structure_rows(structure: DocStructure) -> list[dict[str, Any]]:
    """The document's existing heading list (for an honest ``not_found`` reply)."""
    return [
        {
            "index": int(h["index"]),
            "level": int(h["level"]),
            "text": str(h.get("text") or ""),
            "numbering_label": h.get("numbering_label"),
        }
        for h in structure.headings
    ]


def locate(
    source: bytes | DocStructure,
    selector: str | None,
    *,
    threshold: float | None = None,
) -> LocatorResult:
    """Locate ``selector`` against ``source`` (bytes or a parsed :class:`DocStructure`).

    Args:
        source: the DOCX bytes or an already-parsed :class:`DocStructure`.
        selector: the target selector (「第三部分」/「三、」/「标题含 xxx」…).
        threshold: confidence floor override (defaults to ``CONFIDENCE_THRESHOLD``).

    Returns:
        A :class:`LocatorResult` — exactly one of ``chosen`` (located), an
        ``ambiguity`` list (do not guess), or ``not_found`` with the document's
        real heading structure.
    """
    structure = source if isinstance(source, DocStructure) else inspect_docx(source)
    floor = CONFIDENCE_THRESHOLD if threshold is None else float(threshold)
    spec = parse_selector(selector)
    rows = _structure_rows(structure)

    if spec.mode == "unparsed":
        return LocatorResult(
            selector=str(selector or ""),
            kind="unparsed",
            candidates=[],
            ambiguity=[],
            chosen=None,
            not_found=True,
            structure=rows,
            confidence_threshold=floor,
            error="无法解析目标选择子",
        )

    candidates = _heading_candidates(structure, spec)
    selected, ambiguity = _apply_ambiguity_policy(candidates, floor)
    return LocatorResult(
        selector=str(selector or ""),
        kind=spec.mode,
        candidates=candidates,
        ambiguity=ambiguity,
        chosen=selected[0] if selected else None,
        not_found=not candidates,
        structure=rows,
        confidence_threshold=floor,
    )
