"""Project-root discovery and the ``qa_tmp`` run-directory layout.

This is layer L0 infrastructure. It answers one question that must never depend
on the caller's current directory: *where is the ForgeFlow project root, and
where do this run's QA artifacts live?*

The anchor is this file's own ``__file__``. Walking up from
``<root>/forgeflow/qa/paths.py`` to the nearest ancestor containing **both**
``pyproject.toml`` and ``forgeflow/__init__.py`` yields ``ForgeFlow-main``
deterministically — whether the process was started from the repository root or
from ``ForgeFlow-main/``. No drive letter is ever hard-coded; ``QA_PROJECT_ROOT``
(project root) and ``QA_TMP_DIR`` (artifacts root) override when a deployment
needs a different layout.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

#: Sub-directory under ``tmp_root`` that holds one directory per run.
_QA_TMP_SUBDIR = "qa"


def _looks_like_project_root(path: Path) -> bool:
    """True when ``path`` is a ForgeFlow project root (pyproject + package)."""
    return (path / "pyproject.toml").is_file() and (path / "forgeflow" / "__init__.py").is_file()


def _locate(candidate: Path) -> Path | None:
    """Best-effort project-root search around ``candidate``.

    Order: ``candidate`` itself, then a nested ``ForgeFlow-main/`` child (so a
    repository root passed via ``--root`` still resolves), then every ancestor.
    Returns ``None`` when nothing matches — the caller decides whether that is
    fatal.
    """
    if _looks_like_project_root(candidate):
        return candidate
    nested = candidate / "ForgeFlow-main"
    if _looks_like_project_root(nested):
        return nested
    for parent in candidate.parents:
        if _looks_like_project_root(parent):
            return parent
    return None


def find_project_root(anchor: Path | None = None) -> Path:
    """Return the absolute ForgeFlow project root, or raise ``RuntimeError``.

    ``QA_PROJECT_ROOT`` wins when set (and must itself be a valid root — a
    misconfigured override fails loudly rather than falling back to a guess).
    Otherwise the search starts at ``anchor`` (default: this file) and walks up.
    A ``RuntimeError`` is raised when nothing matches: the path is never guessed.
    """
    env_root = os.environ.get("QA_PROJECT_ROOT", "").strip()
    if env_root:
        resolved = Path(env_root).expanduser().resolve()
        if not _looks_like_project_root(resolved):
            raise RuntimeError(
                f"QA_PROJECT_ROOT={env_root!r} is not a ForgeFlow project root "
                "(needs pyproject.toml + forgeflow/__init__.py)."
            )
        return resolved

    base = (anchor if anchor is not None else Path(__file__)).expanduser().resolve()
    start = base if base.is_dir() else base.parent
    found = _locate(start)
    if found is None:
        raise RuntimeError(
            f"could not locate the ForgeFlow project root from {start!s} (looked for a "
            "directory containing pyproject.toml and forgeflow/__init__.py)."
        )
    return found


class QaPaths:
    """Resolved project root + ``qa_tmp`` layout for one process."""

    def __init__(self, project_root: Path, *, tmp_env: str | None = None) -> None:
        self._project_root = Path(project_root).expanduser().resolve()
        override = tmp_env if tmp_env is not None else os.environ.get("QA_TMP_DIR")
        self._tmp_override: str | None = override.strip() if isinstance(override, str) else None

    @classmethod
    def resolve(cls, root: Path | None = None) -> QaPaths:
        """Build a :class:`QaPaths` from an optional explicit ``root``.

        With no ``root`` the project root is auto-detected. With one, it is
        accepted only if it (or a nested ``ForgeFlow-main``/ancestor) is a real
        project root — otherwise ``RuntimeError``.
        """
        if root is None:
            return cls(find_project_root())
        candidate = Path(root).expanduser().resolve()
        located = _locate(candidate)
        if located is None:
            raise RuntimeError(
                f"{root!s} is not a ForgeFlow project root, and none was found above it."
            )
        return cls(located)

    @property
    def project_root(self) -> Path:
        """Absolute path of the ForgeFlow project root."""
        return self._project_root

    @property
    def tmp_root(self) -> Path:
        """Artifacts root: ``QA_TMP_DIR`` when set, else ``<project_root>/qa_tmp``."""
        if self._tmp_override:
            return Path(self._tmp_override).expanduser()
        return self._project_root / "qa_tmp"

    def run_dir(self, run_id: str) -> Path:
        """Absolute directory for one run: ``<tmp_root>/qa/<run_id>``."""
        return self.tmp_root / _QA_TMP_SUBDIR / run_id

    @staticmethod
    def new_run_id(now: datetime | None = None) -> str:
        """A sortable, unique run id: ``<UTC YYYYMMDDThhmmssZ>-<8 hex chars>``."""
        moment = now if now is not None else datetime.now(UTC)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        stamp = moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        return f"{stamp}-{uuid.uuid4().hex[:8]}"


__all__ = ["QaPaths", "find_project_root"]
