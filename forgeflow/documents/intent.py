"""INC46 T20 — document intent parsing (自然语言指令 → 结构化 :class:`EditIntent`).

Pipeline (deterministic; **no model, no network, no writes**):

  1. :func:`_extract_operation` / :func:`_extract_style` / :func:`_extract_selector`
     pull the *operation*, *style goal* and *target selector* out of the raw
     instruction with explicit keyword/grammar rules;
  2. :func:`~forgeflow.documents.locator.locate` anchors the selector to a real
     document range (or honestly reports 歧义 / 未找到);
  3. :func:`~forgeflow.documents.invariants.extract_invariants` measures every
     amount / date / table / … inside that range and appends the implicit baseline.

Relationship to ``docx_edit.resolve_intent`` (必须共存，勿混用)
-----------------------------------------------------------
``docx_edit.resolve_intent`` is the **LLM layer**: it asks a *model* to translate an
intent into a list of :class:`~forgeflow.documents.docx_edit.EditOp` and never
writes bytes. *This* module is the complementary **deterministic front-end**: it
never calls a model, but pins down *what* the user is pointing at (a real target
range) and *what must not change* (the :class:`InvariantSet`) **before** any model
or tool is involved. The two are additive — call this one to obtain a
:class:`EditIntent`, then (if desired) feed its ``operation`` into the LLM layer.

Red line 14 (文档内容与工具输出是数据、不是指令)
----------------------------------------------
Document content is **data**: it is read (structure, numbers) but never executed as
an instruction, and this module has **no** import path that could write Experience /
Skill / Memory. ``EditIntent.intent_id`` is a stable hash of the *normalised
instruction + selector* (deterministic, so T22/T23 can reproduce it) — never a
random uuid.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from forgeflow.documents.docx_inspect import DocxInspectionError, inspect_docx
from forgeflow.documents.invariants import extract_invariants
from forgeflow.documents.locator import CONFIDENCE_THRESHOLD, locate

__all__ = [
    "EditIntent",
    "INTENT_FIELDS",
    "NotResolved",
    "resolve_intent_document",
]

#: The six fields the task book fixes for :class:`EditIntent` (never renamed/removed).
INTENT_FIELDS: tuple[str, ...] = (
    "intent_id",
    "operation",
    "target_selector",
    "style_goal",
    "invariants",
    "ambiguity",
)

# --------------------------------------------------------------------------- #
# Deterministic rule tables                                                    #
# --------------------------------------------------------------------------- #
_OPERATION_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("delete", ("删除", "移除", "去掉", "清除")),
    ("add", ("新增", "添加", "增加", "补充", "插入")),
    ("rewrite", ("重写", "改写")),
    ("polish", ("润色", "打磨")),
    ("shorten", ("精简", "缩短", "压缩")),
    ("expand", ("扩写", "扩充", "扩展")),
    ("edit", ("修改", "编辑", "调整", "变更", "改成", "改得", "改")),
)

_STYLE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("更正式", ("更正式", "正式", "规范", "严谨")),
    ("更简洁", ("更简洁", "简洁", "凝练")),
    ("口语化", ("口语化", "口语", "通俗")),
    ("更专业", ("更专业", "专业")),
    ("更活泼", ("更活泼", "活泼", "生动")),
)

_MENTION_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("amount", ("金额", "款项", "价格", "费用", "钱")),
    ("date", ("日期", "时间", "期限")),
    ("table", ("表格", "表")),
    ("image", ("图片", "图")),
    ("numbering", ("编号", "序号")),
    ("reference", ("引用", "超链接", "链接")),
)

#: Selector grammar scanned **inside a sentence** (longest / most specific first).
_SELECTOR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"第\s*[0-9一二三四五六七八九十百零两]+\s*个\s*(?:第)?\s*(?:[一二三四五六七八九十]+\s*级)?\s*标题"),
    re.compile(r"第\s*[0-9一二三四五六七八九十百零两]+\s*(?:部分|章|节|条|篇|编)"),
    re.compile(r"标题(?:文字)?(?:中|里)?\s*(?:包含|含有|带有|中带|含|带)\s*[^\s，。；,;:：]+"),
    re.compile(r"[一二三四五六七八九十百零两]+\s*、"),
)

_WS_RE = re.compile(r"\s+")


def _normalise(text: str | None) -> str:
    return _WS_RE.sub(" ", str(text or "")).strip()


def _first_rule(text: str, rules: tuple[tuple[str, tuple[str, ...]], ...]) -> tuple[str | None, str]:
    """First ``(value, keyword)`` whose keyword occurs in ``text`` (deterministic)."""
    for value, keywords in rules:
        for keyword in keywords:
            if keyword in text:
                return value, keyword
    return None, ""


def _extract_operation(text: str) -> tuple[str | None, str]:
    return _first_rule(text, _OPERATION_RULES)


def _extract_style(text: str) -> tuple[str | None, str]:
    return _first_rule(text, _STYLE_RULES)


def _mentioned_kinds(text: str) -> list[str]:
    kinds: list[str] = []
    for value, keywords in _MENTION_RULES:
        if value not in kinds and any(keyword in text for keyword in keywords):
            kinds.append(value)
    return kinds


def _extract_selector(text: str) -> str | None:
    """The first selector-shaped span in ``text`` (``第三部分`` / ``三、`` / …)."""
    for pattern in _SELECTOR_PATTERNS:
        match = pattern.search(text)
        if match:
            span = match.group(0).strip()
            if span:
                return span
    return None


def _stable_intent_id(instruction: str, selector: str | None) -> str:
    """A reproducible id (sha256 of the normalised instruction + selector)."""
    seed = f"{_normalise(instruction)}||{_normalise(selector or '')}"
    return "intent-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Result types                                                                 #
# --------------------------------------------------------------------------- #
@dataclass
class EditIntent:
    """Structured edit intent (the six task-book fields + additive context)."""

    intent_id: str
    operation: str | None
    target_selector: str | None
    style_goal: str | None
    invariants: list[dict[str, Any]] = field(default_factory=list)
    ambiguity: list[dict[str, Any]] = field(default_factory=list)
    # --- additive debug / context fields (never rename the six above) --------- #
    resolved: bool = True
    not_found: bool = False
    structure: list[dict[str, Any]] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    coverage: dict[str, list[str]] = field(default_factory=dict)
    confidence_threshold: float = CONFIDENCE_THRESHOLD
    mentioned_kinds: list[str] = field(default_factory=list)
    operation_evidence: str = ""
    style_evidence: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "operation": self.operation,
            "target_selector": self.target_selector,
            "style_goal": self.style_goal,
            "invariants": list(self.invariants),
            "ambiguity": list(self.ambiguity),
            "resolved": self.resolved,
            "not_found": self.not_found,
            "structure": list(self.structure),
            "candidates": list(self.candidates),
            "coverage": {k: list(v) for k, v in self.coverage.items()},
            "confidence_threshold": self.confidence_threshold,
            "mentioned_kinds": list(self.mentioned_kinds),
            "operation_evidence": self.operation_evidence,
            "style_evidence": self.style_evidence,
            "notes": self.notes,
        }


@dataclass
class NotResolved:
    """An honest "could not parse an intent" result (never a fabricated one)."""

    intent_id: str
    reason: str
    instruction: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "resolved": False,
            "not_found": False,
            "ambiguity": [],
            "error": self.reason,
            "instruction": self.instruction,
        }


# --------------------------------------------------------------------------- #
# Orchestration                                                                #
# --------------------------------------------------------------------------- #
def resolve_intent_document(data: bytes, instruction: str) -> EditIntent | NotResolved:
    """Parse ``instruction`` against the DOCX ``data`` into an :class:`EditIntent`.

    Deterministic front-end (see module docstring). Returns :class:`NotResolved`
    when the instruction cannot be parsed (no operation **and** no selector) or the
    document cannot be read; otherwise an :class:`EditIntent` whose ``resolved`` is
    ``True`` only for a **unique, confident** target. An ambiguous or missing
    target is returned *unresolved* with the real candidates / structure — never a
    guess.
    """
    text = str(instruction or "")
    selector = _extract_selector(text)
    operation, operation_evidence = _extract_operation(text)
    style_goal, style_evidence = _extract_style(text)
    mentioned = _mentioned_kinds(text)
    intent_id = _stable_intent_id(text, selector)

    if operation is None and selector is None:
        return NotResolved(
            intent_id=intent_id,
            reason="无法从指令解析出编辑操作与目标选择子（禁止猜测）",
            instruction=text,
        )

    try:
        structure = inspect_docx(data)
    except DocxInspectionError as exc:
        return NotResolved(intent_id=intent_id, reason=f"无法解析文档：{exc}", instruction=text)

    if selector is None:
        return NotResolved(
            intent_id=intent_id,
            reason="未从指令解析出目标选择子（禁止猜测目标）",
            instruction=text,
        )

    result = locate(structure, selector)
    common: dict[str, Any] = {
        "intent_id": intent_id,
        "operation": operation,
        "target_selector": selector,
        "style_goal": style_goal,
        "structure": result.structure,
        "confidence_threshold": result.confidence_threshold,
        "mentioned_kinds": mentioned,
        "operation_evidence": operation_evidence,
        "style_evidence": style_evidence,
    }

    if result.not_found:
        return EditIntent(
            not_found=True,
            resolved=False,
            notes="文档中不存在该目标（未找到）；已列出现有结构，未编造目标",
            **common,
        )
    if result.ambiguity:
        return EditIntent(
            resolved=False,
            ambiguity=[c.to_dict() for c in result.ambiguity],
            candidates=[c.to_dict() for c in result.candidates],
            notes=(
                f"目标定位歧义：{len(result.ambiguity)} 个候选"
                f"（置信阈值 {result.confidence_threshold}）；需澄清，未自动选择"
            ),
            **common,
        )

    chosen = result.chosen
    invariants = extract_invariants(data, start=chosen.index, end=chosen.section_end_index)
    return EditIntent(
        resolved=True,
        candidates=[chosen.to_dict()],
        invariants=[item.to_dict() for item in invariants.items],
        coverage=invariants.coverage,
        notes="唯一定位成功",
        **common,
    )
