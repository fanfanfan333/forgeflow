"""INC43 S4 — the DOCX artifact blob store (design §4 ruling: bytes ≠ payload).

Why this module exists
----------------------
A real DOCX is tens of kilobytes. The runtime bounds every stored tool
``payload`` at ``tool_executor.MAX_PAYLOAD_CHARS`` (4000) — a base64 DOCX would
therefore be replaced by a ``{"truncated": True, "preview": ...}`` envelope and
the ``content`` key would vanish, so the deliverable could **never** be
projected (a permanent false empty state). So the bytes live here, on disk,
outside the project tree, and the run payload carries only a small
``artifact_ref`` (``"<sha256[:2]>/<sha256>.docx"``).

The store is content-addressed and idempotent (same bytes ⇒ same ref), and the
root is refused when it resolves inside the ForgeFlow checkout — the same
guard-rail idea as ``resources/storage.py::_is_within_project`` (AC-11: a task
may never write the platform tree).
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from forgeflow.config import get_settings

__all__ = ["DocArtifactStore", "DocStorePathError"]

#: The ForgeFlow checkout root — a blob root must never live under it.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class DocStorePathError(ValueError):
    """Raised when the document store root resolves inside the project tree."""


def _is_within_project(path: Path) -> bool:
    """True when ``path`` resolves inside the ForgeFlow checkout."""
    try:
        resolved = path.resolve()
    except OSError:  # pragma: no cover — an unresolvable path is treated as unsafe
        return True
    root = _PROJECT_ROOT.resolve()
    return resolved == root or root in resolved.parents


class DocArtifactStore:
    """Content-addressed, filesystem-backed store for DOCX deliverable bytes."""

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        base = Path(root) if root is not None else get_settings().resource_store_path()
        self._root = Path(base) / "documents"
        if _is_within_project(self._root):
            raise DocStorePathError(
                f"文档产物存储根目录必须位于 ForgeFlow 工程树之外：{self._root}"
            )

    @property
    def root(self) -> Path:
        return self._root

    def _path_for(self, ref: str) -> Path:
        rel = str(ref).replace("\\", "/").lstrip("/")
        if not rel or ".." in rel.split("/"):
            raise ValueError(f"非法产物引用：{ref!r}")
        return self._root / rel

    def put(self, data: bytes) -> str:
        """Persist ``data`` and return its content-addressed ``artifact_ref``.

        Returns ``"<sha256[:2]>/<sha256>.docx"`` — stable and idempotent, so
        re-saving identical bytes yields the same reference.
        """
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise ValueError("DOCX 字节为空")
        payload = bytes(data)
        digest = hashlib.sha256(payload).hexdigest()
        ref = f"{digest[:2]}/{digest}.docx"
        target = self._path_for(ref)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size == len(payload):
            return ref
        # Atomic write: a reader never observes a half-written blob.
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
            os.replace(tmp_name, target)
        finally:
            if os.path.exists(tmp_name):
                try:
                    os.remove(tmp_name)
                except OSError:  # pragma: no cover
                    pass
        return ref

    def get(self, ref: str) -> bytes:
        """Read back the stored bytes for a reference."""
        return self._path_for(ref).read_bytes()

    def exists(self, ref: str) -> bool:
        return self._path_for(ref).exists()
