"""Bounded syntax-definition and text-reference lookup; never claims LSP resolution."""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

_SOURCE_SUFFIXES = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".go",
    ".rs",
    ".java",
}
_EXCLUDED = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}


def locate_symbol(paths: list[str], symbol: str, *, limit: int = 50) -> dict:
    if not isinstance(symbol, str) or not symbol.isidentifier() or len(symbol) > 128:
        raise ValueError("symbol must be one identifier of at most 128 characters")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Navigation limit must be between 1 and 100")
    files = []
    truncated = False
    for raw in paths:
        root = Path(raw)
        if root.is_symlink():
            raise ValueError("Navigation input must not be a symbolic link")
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            for folder, dirs, names in os.walk(root, followlinks=False):
                dirs[:] = sorted(
                    name
                    for name in dirs
                    if name not in _EXCLUDED and not (Path(folder) / name).is_symlink()
                )
                for name in sorted(names):
                    path = Path(folder) / name
                    if path.suffix in _SOURCE_SUFFIXES and not path.is_symlink():
                        files.append(path)
                if len(files) >= 500:
                    truncated = True
                    break
        else:
            raise ValueError("Navigation input is unavailable")
    matches = []
    checked = 0
    pattern = re.compile(r"(?<!\w)" + re.escape(symbol) + r"(?!\w)")
    for path in sorted(set(files))[:500]:
        if path.is_symlink() or path.stat().st_size > 262144:
            truncated = True
            continue
        try:
            source = path.read_text(encoding="utf-8-sig")
        except UnicodeError:
            truncated = True
            continue
        definitions = set()
        if path.suffix in {".py", ""}:
            try:
                definitions = {
                    node.lineno
                    for node in ast.walk(ast.parse(source))
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    and node.name == symbol
                }
            except SyntaxError:
                pass
        checked += 1
        for line_number, line in enumerate(source.splitlines(), 1):
            match = pattern.search(line)
            if match:
                matches.append(
                    {
                        "file": str(path),
                        "line": line_number,
                        "column": match.start(),
                        "kind": "definition" if line_number in definitions else "text_reference",
                        "text": line[max(0, match.start() - 60) : match.start() + 100],
                    }
                )
                if len(matches) >= limit:
                    truncated = True
                    break
        if len(matches) >= limit:
            break
    return {
        "ok": True,
        "provider": "stdlib-ast/text",
        "symbol": symbol,
        "language_service": "not_configured",
        "semantic_resolution": False,
        "files_checked": checked,
        "matches": matches,
        "truncated": truncated,
        "summary": f"定位 {symbol}：{len(matches)} 处语法定义或文本引用；未使用语言服务器",
    }
