"""Code-source registration (INC25 W1, P0-7).

Three code-source kinds are registerable, each optionally with a branch:

  * **GitHub / GitLab repository** — ``source_type`` in ``("github", "gitlab")``.
    A real language/file statistic requires a credential + the provider API; with
    no credential the source is registered as ``unavailable`` with a **verbatim**
    reason (never a fabricated count, never "已就绪").
  * **Local path** — a filesystem directory walked with the standard library. The
    language/file counts are **real** (a deterministic, offline statistic).
  * **ZIP archive** — a ``.zip`` blob read with the standard library; the
    language/file counts are **real** and offline.

Every summary carries ``source_type`` / ``identifier`` / ``branch`` /
``languages`` / ``files_count``; ``languages`` and ``files_count`` are only ever
set from a real read (``0``/``[]`` when nothing was read are honest zeros, and
the reason lives in ``detail``).
"""

from __future__ import annotations

import base64
import binascii
import io
import logging
import os
import zipfile
from pathlib import Path
from typing import Any, Iterable

from forgeflow.config import get_settings
from forgeflow.resources.models import CodeLocator

logger = logging.getLogger(__name__)

__all__ = [
    "CODE_SOURCE_TYPES",
    "EXTENSION_LANGUAGE",
    "summarize_local_path",
    "summarize_zip",
    "summarize_repository",
    "register_code_source",
]

CODE_SOURCE_TYPES: tuple[str, ...] = ("github", "gitlab", "local_path", "zip")

#: Extension → language. Used to count languages over a *real* file listing.
EXTENSION_LANGUAGE: dict[str, str] = {
    ".py": "Python", ".pyi": "Python", ".js": "JavaScript", ".jsx": "JavaScript",
    ".mjs": "JavaScript", ".cjs": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".java": "Java", ".kt": "Kotlin", ".go": "Go", ".rs": "Rust", ".rb": "Ruby",
    ".php": "PHP", ".cs": "C#", ".c": "C", ".h": "C", ".cc": "C++", ".cpp": "C++",
    ".hpp": "C++", ".swift": "Swift", ".scala": "Scala", ".sh": "Shell", ".bash": "Shell",
    ".ps1": "PowerShell", ".sql": "SQL", ".html": "HTML", ".css": "CSS", ".scss": "SCSS",
    ".vue": "Vue", ".r": "R", ".m": "Objective-C", ".lua": "Lua", ".dart": "Dart",
    ".yaml": "YAML", ".yml": "YAML", ".json": "JSON", ".toml": "TOML", ".md": "Markdown",
    ".tf": "Terraform", ".ex": "Elixir", ".exs": "Elixir", ".clj": "Clojure", ".pl": "Perl",
}

_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
        ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".idea",
        ".vscode", "target", "vendor", ".tox", ".next", "coverage", ".gradle",
    }
)


def language_for(path: str) -> str:
    """Language for a file path by extension (``""`` when unknown)."""
    dot = str(path).lower().rfind(".")
    if dot < 0:
        return ""
    return EXTENSION_LANGUAGE.get(str(path).lower()[dot:], "")


def _summarise_names(names: Iterable[str]) -> tuple[int, list[str]]:
    """Count code files + languages over a real list of relative paths."""
    count = 0
    languages: set[str] = set()
    for raw in names:
        rel = str(raw).replace("\\", "/")
        parts = rel.split("/")
        if any(part in _SKIP_DIRS for part in parts[:-1]):
            continue
        if not rel or rel.endswith("/"):
            continue
        language = language_for(rel)
        if language:
            count += 1
            languages.add(language)
    return count, sorted(languages)


def summarize_local_path(path: str) -> CodeLocator:
    """Walk a local directory and report its **real** code-file/language counts."""
    ident = str(path or "").strip()
    locator = CodeLocator(source_type="local_path", identifier=ident)
    if not ident:
        locator.reachable = False
        locator.detail = "未提供本地路径（identifier/path 为空）"
        return locator
    root = Path(ident).expanduser()
    if not root.exists():
        locator.reachable = False
        locator.detail = f"本地路径不存在：{root}"
        return locator
    if not root.is_dir():
        locator.reachable = False
        locator.detail = f"本地路径不是目录：{root}"
        return locator
    names: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        rel_dir = os.path.relpath(dirpath, root)
        for filename in filenames:
            rel = filename if rel_dir in (".", "") else f"{rel_dir}/{filename}"
            names.append(rel)
    count, languages = _summarise_names(names)
    locator.files_count = count
    locator.languages = languages
    locator.reachable = True
    return locator


def summarize_zip(data: bytes, *, filename: str = "") -> CodeLocator:
    """Read a ZIP archive and report its **real** code-file/language counts."""
    locator = CodeLocator(source_type="zip", identifier=filename or "upload.zip")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = [info.filename for info in archive.infolist() if not info.is_dir()]
    except (zipfile.BadZipFile, OSError) as exc:
        locator.reachable = False
        locator.detail = f"ZIP 无法读取：{exc}"
        return locator
    count, languages = _summarise_names(names)
    locator.files_count = count
    locator.languages = languages
    locator.reachable = True
    return locator


def _probe_repository(source_type: str, identifier: str, branch: str) -> CodeLocator:
    """Probe a GitHub/GitLab repository API for a real language/file statistic.

    Requires a credential; without one the source degrades to ``unavailable`` with
    a verbatim reason. Network / provider failures are caught and reported
    verbatim — never swallowed into a fake success.
    """
    locator = CodeLocator(source_type=source_type, identifier=identifier, branch=branch)
    settings = get_settings()
    owner_repo = identifier.strip().strip("/")
    if "/" not in owner_repo:
        locator.reachable = False
        locator.detail = f"仓库标识必须为 owner/repo 形式：{identifier!r}"
        return locator

    ref = branch or ("main")
    try:
        import httpx
    except Exception as exc:  # pragma: no cover — httpx is a hard dependency
        locator.reachable = False
        locator.detail = f"HTTP 客户端不可用：{exc}"
        return locator

    if source_type == "github":
        token = settings.github_token.get_secret_value() if settings.github_token else ""
        if not token:
            locator.reachable = False
            locator.detail = "未配置 GitHub 凭据（Settings.github_token 为空），无法读取仓库元数据"
            return locator
        base = settings.github_base_url.rstrip("/")
        url = f"{base}/repos/{owner_repo}/git/trees/{ref}?recursive=1"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    else:  # gitlab
        token = os.environ.get("GITLAB_TOKEN", "").strip()
        if not token:
            locator.reachable = False
            locator.detail = "未配置 GitLab 凭据（环境变量 GITLAB_TOKEN 为空），无法读取仓库元数据"
            return locator
        base = os.environ.get("GITLAB_BASE_URL", "https://gitlab.com/api/v4").rstrip("/")
        url = f"{base}/projects/{owner_repo.replace('/', '%2F')}/repository/tree?ref={ref}&per_page=100"
        headers = {"PRIVATE-TOKEN": token}

    try:
        response = httpx.get(url, headers=headers, timeout=8.0, follow_redirects=True)
        if response.status_code != 200:
            locator.reachable = False
            locator.detail = (
                f"仓库不可达：{source_type} 返回 HTTP {response.status_code}"
                f"（{url}）"
            )
            return locator
        payload = response.json()
        if source_type == "github":
            names = [
                str(item.get("path") or "")
                for item in (payload.get("tree") or [])
                if item.get("type") == "blob"
            ]
        else:
            names = [str(item.get("path") or "") for item in (payload or []) if item.get("type") == "blob"]
    except Exception as exc:  # noqa: BLE001 — a probe failure is a degrade, never a crash
        locator.reachable = False
        locator.detail = f"仓库探测失败：{exc}"
        return locator

    count, languages = _summarise_names(names)
    locator.files_count = count
    locator.languages = languages
    locator.reachable = True
    return locator


def register_code_source(source: dict[str, Any]) -> tuple[str, CodeLocator, str]:
    """Register a code source from a declarative ``source`` dict.

    Args:
        source: one of
            ``{"source_type": "github"|"gitlab", "identifier": "owner/repo", "branch": "..."}``,
            ``{"source_type": "local_path", "identifier"/"path": "/abs/dir"}``, or
            ``{"source_type": "zip", "filename": "x.zip", "zip_base64": "..."}``
            (``data`` bytes is also accepted for an in-process caller).

    Returns:
        ``(status, locator, detail)`` where ``status`` is the resource status
        (``parsed`` when a real statistic was read, else ``unavailable``).
    """
    source_type = str(source.get("source_type") or "").strip().lower()
    branch = str(source.get("branch") or "").strip()
    if source_type not in CODE_SOURCE_TYPES:
        locator = CodeLocator(source_type=source_type, branch=branch)
        locator.reachable = False
        locator.detail = f"不支持的代码来源类型：{source_type!r}（可选：{', '.join(CODE_SOURCE_TYPES)}）"
        return "unavailable", locator, locator.detail

    if source_type in ("github", "gitlab"):
        identifier = str(
            source.get("identifier") or source.get("repository") or source.get("repo") or ""
        ).strip()
        if not identifier:
            locator = CodeLocator(source_type=source_type, branch=branch)
            locator.reachable = False
            locator.detail = "未提供仓库标识（owner/repo）"
            return "unavailable", locator, locator.detail
        locator = _probe_repository(source_type, identifier, branch)
        status = "parsed" if locator.reachable else "unavailable"
        return status, locator, locator.detail

    if source_type == "local_path":
        identifier = str(source.get("identifier") or source.get("path") or "").strip()
        locator = summarize_local_path(identifier)
        locator.branch = branch
        status = "parsed" if locator.reachable else "unavailable"
        return status, locator, locator.detail

    # zip
    filename = str(source.get("filename") or source.get("name") or "upload.zip").strip()
    data = source.get("data")
    if data is None and source.get("zip_base64"):
        try:
            data = base64.b64decode(str(source["zip_base64"]), validate=False)
        except (binascii.Error, ValueError) as exc:
            locator = CodeLocator(source_type="zip", identifier=filename, branch=branch)
            locator.reachable = False
            locator.detail = f"ZIP 载荷不是合法 base64：{exc}"
            return "unavailable", locator, locator.detail
    if not data:
        locator = CodeLocator(source_type="zip", identifier=filename, branch=branch)
        locator.reachable = False
        locator.detail = "未提供 ZIP 内容（zip_base64/data 为空）"
        return "unavailable", locator, locator.detail
    locator = summarize_zip(bytes(data), filename=filename)
    locator.branch = branch
    status = "parsed" if locator.reachable else "unavailable"
    return status, locator, locator.detail
