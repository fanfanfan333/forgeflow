"""File blob storage for resource uploads (INC25 W1).

``FileBlobStore`` content-addresses uploaded bytes under
``Settings.resource_store_path()`` (which MUST resolve outside the ForgeFlow
project tree — a code task may never write the platform checkout, AC-11). The
reference it returns (``"<sha256[:2]>/<sha256>"``) is what a
``FileLocator.storage_ref`` holds, so a resource can be re-read and byte-for-byte
verified later (AC-2).

The single-file size ceiling is checked here against
``Settings.multimodal_max_bytes``: an over-limit upload is a **client** error and
raises :class:`ResourceTooLargeError` (the router maps it to HTTP 413, with the
actual byte count and the limit both present in the message — AC-6). A file of an
unsupported type raises :class:`UnsupportedResourceTypeError` (HTTP 400) BEFORE
any bytes are persisted, so no "已解析" entry is ever created for it (AC-7).
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from pathlib import Path

from forgeflow.config import get_settings

logger = logging.getLogger(__name__)

__all__ = [
    "ResourceTooLargeError",
    "UnsupportedResourceTypeError",
    "FileBlobStore",
]

#: The ForgeFlow checkout root — a blob/workspace root must never live under it.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ResourceTooLargeError(ValueError):
    """Raised when an upload exceeds ``Settings.multimodal_max_bytes``.

    The message contains **both** numbers (actual + limit) so the HTTP 413
    ``detail`` is verbatim-honest (AC-6).
    """

    def __init__(self, actual_bytes: int, limit_bytes: int) -> None:
        self.actual_bytes = int(actual_bytes)
        self.limit_bytes = int(limit_bytes)
        super().__init__(
            f"文件为 {self.actual_bytes} 字节，超过单文件上限 {self.limit_bytes} 字节"
        )


class UnsupportedResourceTypeError(ValueError):
    """Raised when a file's type has no resource-summary path (client error)."""


def _is_within_project(path: Path) -> bool:
    """True when ``path`` resolves inside the ForgeFlow checkout."""
    try:
        resolved = path.resolve()
    except OSError:  # pragma: no cover — unresolvable path is treated as unsafe
        return True
    root = _PROJECT_ROOT.resolve()
    return resolved == root or root in resolved.parents


class FileBlobStore:
    """Content-addressed, filesystem-backed store for resource bytes."""

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        base = Path(root) if root is not None else get_settings().resource_store_path()
        self._root = Path(base)
        if _is_within_project(self._root):
            # A misconfigured root is a real hazard, not a warning: refuse it
            # rather than silently writing user content into the repo.
            raise ValueError(
                f"resource store root must be outside the ForgeFlow project tree: {self._root}"
            )

    @property
    def root(self) -> Path:
        return self._root

    def _path_for(self, storage_ref: str) -> Path:
        rel = str(storage_ref).replace("\\", "/").lstrip("/")
        if ".." in rel.split("/"):
            raise ValueError(f"invalid storage_ref: {storage_ref!r}")
        return self._root / rel

    def check_size(self, data: bytes, *, max_bytes: int | None = None) -> None:
        """Raise :class:`ResourceTooLargeError` when ``data`` exceeds the ceiling."""
        limit = int(max_bytes) if max_bytes is not None else int(get_settings().multimodal_max_bytes)
        if len(data) > limit:
            raise ResourceTooLargeError(len(data), limit)

    def put(self, name: str, data: bytes, *, max_bytes: int | None = None) -> str:
        """Persist ``data`` and return its content-addressed storage reference.

        Args:
            name: original file name (kept for the locator; not used in the path).
            data: the raw bytes to store.
            max_bytes: ceiling override (defaults to ``multimodal_max_bytes``).

        Returns:
            ``"<sha256[:2]>/<sha256>"`` — a stable, content-addressed reference.
        """
        self.check_size(data, max_bytes=max_bytes)
        digest = hashlib.sha256(data).hexdigest()
        storage_ref = f"{digest[:2]}/{digest}"
        target = self._path_for(storage_ref)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size == len(data):
            return storage_ref
        # Atomic write: a reader never observes a half-written blob.
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp_name, target)
        finally:
            if os.path.exists(tmp_name):
                try:
                    os.remove(tmp_name)
                except OSError:  # pragma: no cover
                    pass
        logger.debug("resource blob stored | ref=%s bytes=%d", storage_ref, len(data))
        return storage_ref

    def resolve(self, storage_ref: str) -> Path:
        """Absolute filesystem path for a reference."""
        return self._path_for(storage_ref)

    def read(self, storage_ref: str) -> bytes:
        """Read back the stored bytes for a reference."""
        return self._path_for(storage_ref).read_bytes()

    def exists(self, storage_ref: str) -> bool:
        return self._path_for(storage_ref).exists()
