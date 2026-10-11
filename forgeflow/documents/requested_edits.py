"""Literal user replacements are executable constraints, not model suggestions."""

from __future__ import annotations

import re

from forgeflow.documents.docx_edit import EditOp, UnknownEditOpError
from forgeflow.documents.docx_inspect import open_docx


def _quoted_spans(text: str) -> list[tuple[int, int, str]]:
    pairs = {"「": "」", "“": "”", '"': '"'}
    spans: list[tuple[int, int, str]] = []
    index = 0
    while index < len(text):
        opener = text[index]
        if opener not in pairs:
            index += 1
            continue
        start = index
        closer = pairs[opener]
        depth = 1
        index += 1
        while index < len(text):
            if text[index] == closer:
                depth -= 1
                if depth == 0:
                    spans.append((start, index + 1, text[start + 1 : index]))
                    break
            elif text[index] == opener:
                depth += 1
            index += 1
        index += 1
    return spans


def requested_replacements(data: bytes, intent: str) -> list[EditOp]:
    """Resolve quoted literal substitutions from the trusted original request.

    Title-specific substitutions retain the rest of that paragraph. Missing or
    ambiguous title targets fail rather than silently choosing another place.
    Other instructions continue through the normal model editing path.
    """
    spans = _quoted_spans(intent)
    paragraphs = list(open_docx(data).paragraphs)
    ops: list[EditOp] = []
    for left, right in zip(spans, spans[1:], strict=False):
        start, end, old = left
        next_start, _, new = right
        if not old or not re.fullmatch(
            r"\s*(?:替换为|替换成|改为|改成|replace\s+with)\s*", intent[end:next_start], re.I
        ):
            continue
        prefix_start = max(intent.rfind("；", 0, start), intent.rfind("。", 0, start)) + 1
        prefix = intent[prefix_start:start].rsplit("；", 1)[-1]
        # Each numbered instruction is a separate scope.
        prefix = re.split(r"\d+[.、]", prefix)[-1]
        if "标题" in prefix[-24:]:
            candidates = [
                i
                for i, p in enumerate(paragraphs)
                if old in p.text and (i == 0 or (p.style and p.style.name == "Title"))
            ]
            if len(candidates) != 1:
                raise UnknownEditOpError("标题替换目标缺失或存在歧义，未生成文档")
            index = candidates[0]
            ops.append(EditOp(op="replace_text", index=index, match=old, replace=new))
        else:
            if not any(old in p.text for p in paragraphs):
                raise UnknownEditOpError(f"原始需求中的替换目标未找到：{old[:80]}")
            ops.append(EditOp(op="replace_text", match=old, replace=new))
    return ops


def preserves_numbers(intent: str) -> bool:
    return bool(
        re.search(
            r"(?:保留|保持|不修改|不改变|不得修改|不要修改|禁止更改)[^。；\n]{0,100}(?:数字|金额|日期)|"
            r"(?:数字|金额|日期)[^。；\n]{0,40}(?:不变|保留)|preserve.{0,40}numbers",
            intent,
            re.I,
        )
    )


def literal_result_matches(original: bytes, edited: bytes, ops: list[EditOp]) -> bool:
    """Independently compare actual paragraph text with the literal user edits."""
    expected = [p.text for p in open_docx(original).paragraphs]
    for op in ops:
        if op.index is not None:
            expected[op.index] = expected[op.index].replace(op.match, op.replace)
        else:
            expected = [text.replace(op.match, op.replace) for text in expected]
    return expected == [p.text for p in open_docx(edited).paragraphs]
