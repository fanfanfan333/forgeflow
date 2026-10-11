"""INC44 §2.2/§1.3 — the text / code file edit plane (stdlib only).

Same three-layer split as the document planes (design §8), so the model can
**never** write a file:

  * :func:`resolve_intent` — the **LLM layer**. Natural language → a list of
    :class:`TextEditOp`, and it *never* touches the file bytes. Untrusted output:
    an unknown ``op`` (or an unparseable reply, or a missing model) yields
    ``None``.
  * :func:`apply_edits` — the **Tool layer**. The only writer. It re-encodes with
    the **same encoding + BOM presence** and the **same EOL style** as the input
    (a CRLF file stays CRLF; a GB18030 file stays GB18030), so an edit never
    silently rewrites the whole file's byte conventions.
  * :func:`compute_diff` — a **pure function** over two byte strings, computed by
    the shared :mod:`forgeflow.documents.textdiff` engine over the file's line
    sequence, so every figure is recomputable.

Supported ops (exactly these; anything else raises :class:`UnknownEditOpError`)::

    replace_text   {"op": "replace_text", "match": "原", "replace": "新"}
    replace_lines  {"op": "replace_lines", "start": 0, "end": 2, "lines": ["新1", "新2"]}
    insert_after   {"op": "insert_after", "match": "锚点行", "lines": ["新增行"]}
    delete_match   {"op": "delete_match", "match": "要删除的整行"}
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from forgeflow.documents.docx_edit import UnknownEditOpError
from forgeflow.documents.docx_inspect import numbers_in_text
from forgeflow.documents.textdiff import DiffReport, diff_counts
from forgeflow.documents.textfile_inspect import (
    _BOM_UTF8,
    TextFileStructure,
    detect_eol,
)

__all__ = [
    "SUPPORTED_OPS",
    "TextEditOp",
    "UnknownEditOpError",
    "apply_edits",
    "compute_diff",
    "resolve_intent",
]

#: The op kinds the platform supports. Anything else is refused explicitly.
SUPPORTED_OPS: tuple[str, ...] = (
    "replace_text",
    "replace_lines",
    "insert_after",
    "delete_match",
    "normalize_whitespace",
)


# ``UnknownEditOpError`` is the **one** shared error class for every edit plane
# (docx / pptx / textfile): re-using ``docx_edit``'s definition (imported above)
# keeps ``documents.UnknownEditOpError`` a single catch target across formats.


def _decode(data: bytes) -> tuple[str, str]:
    """Re-use the resource seam's single honest decoder."""
    from forgeflow.resources.summaries import _decode as _resource_decode

    return _resource_decode(data)


# --------------------------------------------------------------------------- #
# TextEditOp                                                                   #
# --------------------------------------------------------------------------- #
@dataclass
class TextEditOp:
    """One text / code edit intention (the LLM layer's only output type)."""

    op: str = ""
    match: str = ""
    replace: str = ""
    start: int | None = None
    end: int | None = None
    lines: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.op not in SUPPORTED_OPS:
            raise UnknownEditOpError(
                f"不支持的编辑操作：{self.op!r}（仅支持 {', '.join(SUPPORTED_OPS)}）"
            )
        if self.op == "replace_text" and not self.match:
            raise UnknownEditOpError("replace_text 需要非空的 match")
        if self.op == "replace_lines" and self.start is None:
            raise UnknownEditOpError("replace_lines 需要 start")
        if self.op in ("insert_after", "delete_match") and not self.match:
            raise UnknownEditOpError(f"{self.op} 需要非空的 match")

    @classmethod
    def from_dict(cls, data: Any) -> TextEditOp:
        """Build an op from a mapping; raise :class:`UnknownEditOpError` on junk."""
        if isinstance(data, TextEditOp):
            return data
        if not isinstance(data, dict):
            raise UnknownEditOpError(f"编辑操作必须是对象：{data!r}")

        def _opt_int(key: str) -> int | None:
            raw = data.get(key)
            if raw is None:
                return None
            try:
                return int(raw)
            except (TypeError, ValueError) as exc:
                raise UnknownEditOpError(f"{key} 非法：{raw!r}") from exc

        raw_lines = data.get("lines")
        lines: list[str] = []
        if isinstance(raw_lines, (list, tuple)):
            lines = [str(line) for line in raw_lines]
        elif raw_lines is not None:
            raise UnknownEditOpError(f"lines 必须是数组：{raw_lines!r}")
        return cls(
            op=str(data.get("op") or "").strip(),
            match=str(data.get("match") or ""),
            replace=str(data.get("replace") or ""),
            start=_opt_int("start"),
            end=_opt_int("end"),
            lines=lines,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "match": self.match,
            "replace": self.replace,
            "start": self.start,
            "end": self.end,
            "lines": list(self.lines),
        }


def _coerce_op(item: Any) -> TextEditOp:
    """Accept a :class:`TextEditOp` or a raw mapping; refuse anything else."""
    if isinstance(item, TextEditOp):
        return item
    if isinstance(item, dict):
        return TextEditOp.from_dict(item)
    raise UnknownEditOpError(f"编辑操作必须是对象或 TextEditOp：{item!r}")


# --------------------------------------------------------------------------- #
# Encoding / EOL preservation                                                  #
# --------------------------------------------------------------------------- #
def _apply_eol(text: str, eol: str) -> str:
    """Re-apply the original line-ending style to an LF-normalised string."""
    if eol == "crlf":
        return text.replace("\n", "\r\n")
    if eol == "cr":
        return text.replace("\n", "\r")
    return text


def _encode(text: str, encoding: str, has_bom: bool) -> bytes:
    """Re-encode text with the **same** encoding + BOM presence as the input.

    UTF-8 (with or without BOM) is the common case; GB18030 is re-encoded in kind
    (falling back to UTF-8 only if a character is outside its repertoire, an
    honest last resort); a lossy Latin-1 decode re-encodes with replacement so a
    decode that already lost characters is not compounded by an exception.
    """
    if encoding in ("utf-8-sig", "utf-8"):
        out = text.encode("utf-8")
    elif encoding == "gb18030":
        try:
            out = text.encode("gb18030")
        except UnicodeEncodeError:  # pragma: no cover — rare, honest fallback
            out = text.encode("utf-8")
    else:  # "latin-1(replace)"
        out = text.encode("latin-1", errors="replace")
    if has_bom:
        out = _BOM_UTF8 + out
    return out


# --------------------------------------------------------------------------- #
# Tool layer — the ONLY writer                                                #
# --------------------------------------------------------------------------- #
def apply_edits(data: bytes, edits: list[Any], *, filename: str = "") -> tuple[bytes, int]:
    """Apply ``edits`` to ``data``; return ``(new_bytes, n_changes)``.

    The file is normalised to LF for editing, edited line-by-line, then written
    back with the original EOL style / encoding / BOM. ``n_changes`` counts the
    real places touched:

      * ``replace_text`` — occurrences of ``match`` replaced;
      * ``replace_lines`` — positions re-written (``max(old, new)``);
      * ``insert_after`` — lines inserted;
      * ``delete_match`` — lines removed.
    """
    raw = bytes(data)
    text, encoding = _decode(raw)
    has_bom = raw.startswith(_BOM_UTF8)
    eol = detect_eol(text)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    ops = [_coerce_op(item) for item in (edits or [])]
    total = 0
    for op in ops:
        if op.op == "normalize_whitespace":
            from pathlib import Path

            from forgeflow.documents.whitespace import normalize_whitespace

            is_plain_text = Path(filename or "").suffix.lower() in {".txt", ".text", ".csv", ".tsv"}
            cleaned = [
                normalize_whitespace(line) if is_plain_text else line.rstrip(" \t")
                for line in lines
            ]
            total += sum(old != new for old, new in zip(lines, cleaned, strict=True))
            lines = cleaned
        if op.op == "replace_text":
            joined = "\n".join(lines)
            if op.match and op.match in joined:
                total += joined.count(op.match)
                lines = joined.replace(op.match, op.replace).split("\n")
        elif op.op == "replace_lines":
            start = max(0, int(op.start))
            end = start if op.end is None else max(start, int(op.end))
            replaced = lines[start:end]
            if replaced != list(op.lines):
                total += max(len(replaced), len(op.lines))
                lines[start:end] = list(op.lines)
        elif op.op == "insert_after":
            idx = next((i for i, line in enumerate(lines) if line == op.match), None)
            if idx is not None and op.lines:
                lines[idx + 1 : idx + 1] = list(op.lines)
                total += len(op.lines)
        elif op.op == "delete_match":
            if op.match:
                kept = [line for line in lines if line != op.match]
                removed = len(lines) - len(kept)
                if removed:
                    total += removed
                    lines = kept

    candidate = "\n".join(lines)
    # Code is often uploaded as .txt. Reject this known invalid transformation
    # before storing an artifact; this scope check is not a compiler substitute.
    if re.search(r"^\s*#\s*include\b|\bmain\s*\([^)]*\)\s*\{", text, re.M):
        tokens = re.sub(
            r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\x27(?:\\.|[^\x27\\])*\x27',
            "",
            candidate,
            flags=re.S,
        )
        depth = 0
        for token in re.findall(r"\breturn\b|[{}]", tokens):
            if token == "{":
                depth += 1
            elif token == "}":
                depth -= 1
                if depth < 0:
                    raise UnknownEditOpError(
                        "C/C++ 代码大括号不匹配，未生成修改文件；请保留完整函数结构"
                    )
            elif depth <= 0:
                raise UnknownEditOpError(
                    "C/C++ 代码中 return 被放在函数外；请读取源码，将 return 放在目标函数结束大括号之前，未生成修改文件"
                )
        if depth != 0:
            raise UnknownEditOpError(
                "C/C++ 代码大括号不匹配：增加 return 时不能删除函数结束大括号，未生成修改文件"
            )
    new_text = _apply_eol(candidate, eol)
    return _encode(new_text, encoding, has_bom), total


# --------------------------------------------------------------------------- #
# Diff (pure)                                                                  #
# --------------------------------------------------------------------------- #
def compute_diff(old: bytes, new: bytes) -> DiffReport:
    """Diff two text files by their line sequence (pure function).

    EOL-agnostic (``str.splitlines`` handles LF / CRLF / CR), so a pure
    encoding-preservation edit reports zero changes. Reuses the shared
    :func:`textdiff.diff_counts` engine and the single numeric rule.
    """
    old_text, _ = _decode(bytes(old))
    new_text, _ = _decode(bytes(new))
    return diff_counts(old_text.splitlines(), new_text.splitlines(), numbers_in_text)


# --------------------------------------------------------------------------- #
# LLM layer (intent → ops). Never writes bytes.                                #
# --------------------------------------------------------------------------- #
def resolve_main_return(data: bytes, intent: str) -> list[TextEditOp] | None:
    """Insert the explicitly requested main return, preserving its scope."""
    if not re.search(r"\breturn\b|返回(?:值)?(?:的)?语句", intent or "", re.I):
        return None
    if re.search(r"\breturn\s+(?!0\b)[\w\"']", intent, re.I):
        return None
    if not re.search(
        r"增加|添加|追加|再加|加上|加一|加个|补|缺|没有|未.*返回|add|insert|missing", intent, re.I
    ) or re.search(r"(?:删除|移除|不要|remove|delete).{0,8}(?:return|返回)", intent, re.I):
        return None
    text, _ = _decode(data)
    match = re.search(r"\bmain\s*\([^)]*\)\s*\{", text)
    if not match:
        return None
    if re.search(r"\bvoid\s+$", text[: match.start()]):
        return None
    masked = re.sub(
        r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\x27(?:\\.|[^\x27\\])*\x27',
        lambda m: " " * len(m.group()),
        text,
        flags=re.S,
    )
    depth = 1
    start = match.end()
    for index in range(start, len(masked)):
        if masked[index] == "{":
            depth += 1
        elif masked[index] == "}":
            depth -= 1
            if depth == 0:
                if re.search(r"\breturn\b", masked[start:index]):
                    return None
                body = text[match.start() : index + 1]
                eol = "\r\n" if "\r\n" in text else "\n"
                updated = body[:-1].rstrip() + eol + "    return 0;" + eol + "}"
                # Editing uses LF anchors; encoding restores the source EOL.
                return [
                    TextEditOp(
                        op="replace_text",
                        match=body.replace("\r\n", "\n").replace("\r", "\n"),
                        replace=updated.replace("\r\n", "\n").replace("\r", "\n"),
                    )
                ]
    return None


def main_return_satisfied(data: bytes) -> bool:
    """Check a real return 0 at main-body depth, excluding comments/strings."""
    text, _ = _decode(data)
    masked = re.sub(
        r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\x27(?:\\.|[^\x27\\])*\x27',
        lambda match: " " * len(match.group()),
        text,
        flags=re.S,
    )
    main = re.search(r"\bmain\s*\([^)]*\)\s*\{", masked)
    if main is None:
        return False
    depth = 1
    for token in re.findall(r"\breturn\s+0\s*;|[{}]", masked[main.end() :]):
        if token == "{":
            depth += 1
        elif token == "}":
            depth -= 1
            if depth == 0:
                break
        elif depth == 1:
            return True
    return False


def apply_requested_edits(
    data: bytes,
    ops: list[TextEditOp],
    intent: str,
    filename: str,
) -> tuple[bytes, int]:
    """Execute model edits and independently enforce explicit compound requests."""
    required = resolve_main_return(data, intent)
    compound = bool(re.search(r"优化|改进|修复|纠正|optimi[sz]e|improve|fix", intent, re.I))
    if required is None:
        return apply_edits(data, ops, filename=filename)
    if not compound:
        return apply_edits(data, required, filename=filename)
    # A closing-brace replacement whose only intended addition is return 0
    # delegates placement to the scope resolver. Never trust the model's
    # placement outside the function, and never salvage arbitrary broken code.
    ops = [
        op
        for op in ops
        if not (
            op.op == "replace_text"
            and op.match.strip() == "}"
            and re.fullmatch(r"\s*(?:}\s*return\s+0\s*;|return\s+0\s*;\s*}?)\s*", op.replace)
        )
    ]
    # Modernise a legacy implicit-int, argument-free entry point only when
    # optimisation was requested; retain all existing body logic.
    text, _ = _decode(data)
    legacy = re.search(r"(?m)^([ \t]*)main\s*\(\s*\)(?=\s*\{)", text)
    if legacy and not any("main" in op.match for op in ops):
        ops = [
            TextEditOp(
                op="replace_text", match=legacy.group(), replace=legacy.group(1) + "int main(void)"
            ),
            *ops,
        ]
    edited, changes = apply_edits(data, ops, filename=filename)
    if ops and changes == 0:
        raise UnknownEditOpError("优化操作未匹配实际源码，未生成产物")
    repair = resolve_main_return(edited, intent)
    if repair is not None:
        edited, added = apply_edits(edited, repair, filename=filename)
        changes += added
    if not main_return_satisfied(edited):
        raise UnknownEditOpError("组合需求未完成：main 函数内缺少 return 0;，未生成产物")
    return edited, changes


_EDIT_SYSTEM = (
    "你是企业多智能体平台（ForgeFlow）的文本/代码文件编辑意图解析器。"
    "你的唯一职责是把自然语言编辑需求解析成结构化的编辑操作数组，"
    "绝不输出文件内容之外的任何内容。"
    "先阅读文件完整结构再决定修改位置。即使扩展名是 txt，内容也可能是 C/C++ 等源码。"
    "增加代码必须保持语法与作用域：return 语句必须放在目标函数结束大括号之前，不能追加到文件末尾或函数外。"
    "用户要求合理增加时须根据现有逻辑选择有意义的改动，不能机械拼接文字；目标不明确时不要随意修改。"
)

_EDIT_INSTRUCTION = (
    "把下面的自然语言编辑需求解析为一个编辑操作数组。\n"
    "只允许这些 op：\n"
    "1. replace_text：子串替换 —— "
    '{{"op":"replace_text","match":"原文子串","replace":"新子串"}}\n'
    "2. replace_lines：按行区间替换（start/end 为 0 基、半开区间）—— "
    '{{"op":"replace_lines","start":0,"end":2,"lines":["新第 1 行","新第 2 行"]}}\n'
    "3. insert_after：在某行之后插入 —— "
    '{{"op":"insert_after","match":"锚点整行","lines":["新增行"]}}\n'
    "4. delete_match：删除匹配的整行 —— "
    '{{"op":"delete_match","match":"要删除的整行"}}\n'
    "只输出一个 JSON 对象，形如：\n"
    '{{"edits": [ ... ]}}\n'
    "文件前若干行：\n{structure}\n"
    "编辑需求：{intent}"
)


def _extract_json(text: str | None) -> dict[str, Any] | None:
    """Lenient first-JSON-object parse (tolerates ``` fences and prose)."""
    if not text:
        return None
    body = str(text).strip()
    if body.startswith("```"):
        body = body.strip("`")
        if body[:4].lower() == "json":
            body = body[4:]
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(body[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _structure_summary(structure: TextFileStructure) -> str:
    lines = [
        f"行数：{structure.lines}；编码：{structure.encoding}；换行：{structure.eol}；"
        f"BOM：{'有' if structure.has_bom else '无'}"
    ]
    for index, text in enumerate(structure.preview):
        lines.append(f"[{index}] {text}")
    return "\n".join(lines)


async def resolve_intent(
    data: bytes, intent: str, structure: TextFileStructure
) -> list[TextEditOp] | None:
    """Parse a natural-language intent into edit ops via the configured model.

    **This function never writes bytes** — it is the LLM layer. It returns
    ``None`` (an honest "no usable intent") when the model is unavailable, the
    reply cannot be parsed, or an op is not in :data:`SUPPORTED_OPS`.
    """
    if not str(intent or "").strip():
        return None
    try:
        from forgeflow.models.provider import get_model

        model = get_model()
    except Exception:  # noqa: BLE001
        return None
    if model is None:
        return None

    try:
        if hasattr(model, "reasoning"):
            model = model.model_copy(update={"reasoning": False})
    except Exception:  # noqa: BLE001
        pass

    prompt = _EDIT_INSTRUCTION.format(
        structure=_structure_summary(structure), intent=str(intent)[:1200]
    )
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        message = await model.ainvoke(
            [SystemMessage(content=_EDIT_SYSTEM), HumanMessage(content=prompt)]
        )
    except Exception:  # noqa: BLE001
        return None

    payload = _extract_json(getattr(message, "content", ""))
    if not isinstance(payload, dict):
        return None
    raw_edits = payload.get("edits")
    if not isinstance(raw_edits, list) or not raw_edits:
        return None
    ops: list[TextEditOp] = []
    for item in raw_edits:
        try:
            ops.append(_coerce_op(item))
        except UnknownEditOpError:
            return None
    return ops or None
