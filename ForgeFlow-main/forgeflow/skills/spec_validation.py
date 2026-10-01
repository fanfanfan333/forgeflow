"""Skill spec ``io_schema`` structural validation (dependency-free).

The four-element skill spec (``prompt`` / ``steps`` / ``tools`` / ``io_schema``)
is stored **verbatim** in ``skill_candidates.draft_spec`` (JSONB) and
``skill_versions.spec``. ``io_schema`` is an *author-declared* descriptor: the
Skill Hub UI edits it as JSON text, so the API must reject a **structurally
impossible** declaration with a readable reason instead of persisting garbage
that a future deterministic executor would choke on.

Design (deliberately small — INC34 constraint "最小后端增量"):

* **No third-party dependency.** ``pyproject.toml`` declares no JSON-schema
  library, and we must not add a heavyweight one, so this is a hand-written,
  bounded walk over a *restricted* subset — never a full Draft-07 engine.
* **Backward compatible.** A spec that does **not** declare ``io_schema`` is
  untouched by the caller; and any free-form object whose *known* keys are
  well-typed never trips the check. Only a value that is structurally
  impossible for the declared subset is reported. Unknown keys are ignored on
  purpose, so the existing ``{"input": {"<field>": "<type>"}, "output": {...}}``
  convention (produced by ``candidate_compiler._infer_io_schema``) always
  passes.
* **Honest, Chinese, human-readable** reasons — the API returns them verbatim
  (design rule: the cause must reach the user, never a vague "参数错误").

Accepted shape (every key optional):

    {
      "input":  { <field>: <type> | <sub-schema>, ... },   # domain convention
      "output": { <field>: <type> | <sub-schema>, ... },   # domain convention
      "type": "object",                                     # JSON-Schema subset
      "properties": { "<name>": <sub-schema>, ... },
      "required": ["<name>", ...],
      "items": <sub-schema>,
      "additionalProperties": true | false | <sub-schema>,
      "enum": [ ... ],
      "description" | "title" | "format" | "default" | "examples" | "$schema": any
    }
"""

from __future__ import annotations

from typing import Any

__all__ = ["validate_io_schema"]

#: Bounded walk — a pathological (or hostile) deeply-nested schema degrades to a
#: single honest error rather than an unbounded recursion / runaway payload.
_MAX_DEPTH = 24
#: Never return a wall of errors; the first few are enough to fix the document.
_MAX_ERRORS = 20

#: JSON-Schema primitive type names we recognise under a ``type`` keyword.
_ALLOWED_TYPES = (
    "object",
    "array",
    "string",
    "number",
    "integer",
    "boolean",
    "null",
)


def _kind(value: Any) -> str:
    """A short Chinese name for a value's runtime type (for error text)."""
    if isinstance(value, bool):
        return "布尔值"
    if value is None:
        return "空值"
    if isinstance(value, dict):
        return "对象"
    if isinstance(value, list):
        return "数组"
    if isinstance(value, str):
        return "字符串"
    if isinstance(value, (int, float)):
        return "数字"
    return type(value).__name__


def _check_node(node: Any, path: str, errors: list[str], depth: int) -> None:
    """Validate one schema node; append Chinese problems to ``errors``."""
    if len(errors) >= _MAX_ERRORS:
        return
    if depth > _MAX_DEPTH:
        errors.append(f"{path} 嵌套层级过深（超过 {_MAX_DEPTH} 层）")
        return
    if not isinstance(node, dict):
        errors.append(f"{path} 必须是对象，实际为{_kind(node)}")
        return

    # Domain convention: `input` / `output` are themselves schema nodes.
    for key in ("input", "output"):
        if key in node:
            _check_node(node[key], f"{path}.{key}", errors, depth + 1)

    if "type" in node:
        declared = node["type"]
        if isinstance(declared, str):
            names = [declared]
        elif isinstance(declared, list) and all(isinstance(x, str) for x in declared):
            names = declared
        else:
            names = []
            errors.append(
                f"{path}.type 必须是字符串或字符串数组，实际为{_kind(declared)}"
            )
        for name in names:
            if name not in _ALLOWED_TYPES:
                errors.append(
                    f"{path}.type 含未知类型 '{name}'"
                    f"（允许：{'、'.join(_ALLOWED_TYPES)}）"
                )

    if "properties" in node:
        props = node["properties"]
        if not isinstance(props, dict):
            errors.append(
                f"{path}.properties 必须是对象（字段名 → 子结构），实际为{_kind(props)}"
            )
        else:
            for name, sub in props.items():
                _check_node(sub, f"{path}.properties.{name}", errors, depth + 1)

    if "required" in node:
        req = node["required"]
        if not isinstance(req, list) or not all(isinstance(x, str) for x in req):
            errors.append(f"{path}.required 必须是字符串数组，实际为{_kind(req)}")

    if "items" in node:
        _check_node(node["items"], f"{path}.items", errors, depth + 1)

    if "additionalProperties" in node:
        additional = node["additionalProperties"]
        if not isinstance(additional, bool):
            _check_node(
                additional, f"{path}.additionalProperties", errors, depth + 1
            )

    if "enum" in node and not isinstance(node["enum"], list):
        errors.append(f"{path}.enum 必须是数组，实际为{_kind(node['enum'])}")


def validate_io_schema(io_schema: Any) -> list[str]:
    """Return the structural problems in ``io_schema`` (empty ⇒ acceptable).

    Args:
        io_schema: The author-declared descriptor. Must be a JSON object; an
            object whose *known* keys are well-typed may carry arbitrary extra
            keys (kept backward compatible with existing free-form specs).

    Returns:
        A list of human-readable Chinese messages. An empty list means the
        document is structurally acceptable (it says nothing about whether the
        skill's *business* meaning is correct).
    """
    errors: list[str] = []
    _check_node(io_schema, "io_schema", errors, 0)
    return errors
