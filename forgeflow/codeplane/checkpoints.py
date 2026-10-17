"""Control-plane file snapshots. No command replay or remote-system rollback."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from forgeflow.documents.store import DocArtifactStore

if TYPE_CHECKING:
    from forgeflow.codeplane.workspace import Workspace

MAX_FILES = 4096
MAX_BYTES = 64 * 1024 * 1024
SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}


def file_hash(path: Path) -> str | None:
    if not path.is_file():
        return None
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_target(root: Path, relative: str) -> Path:
    parts = relative.split("/")
    if (not relative or "\\" in relative or ":" in relative or "\x00" in relative
            or PurePosixPath(relative).is_absolute()
            or any(part in {"", ".", ".."} or part in SKIP_DIRS for part in parts)):
        raise ValueError("Invalid checkpoint file path")
    target = root.joinpath(*parts)
    if not target.resolve().is_relative_to(root.resolve()) or any(
        p.is_symlink() for p in (target, *target.parents) if p != root.parent
    ):
        raise ValueError("Checkpoint file path must not cross a symlink")
    return target


def capture(workspace: Workspace, store: DocArtifactStore) -> dict:
    root = Path(workspace.path)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("Workspace files are unavailable")
    files = {}
    total = 0
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        if any((Path(directory) / d).is_symlink() for d in dirs):
            raise ValueError("Cannot snapshot a workspace containing symlinks")
        for name in sorted(names):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            path = safe_target(root, relative)
            if not path.is_file():
                raise ValueError("Checkpoint accepts regular files only")
            if len(files) >= MAX_FILES or path.stat().st_size > MAX_BYTES - total:
                raise ValueError("Workspace exceeds checkpoint limits")
            before = path.stat()
            data = path.read_bytes()
            after = path.stat()
            if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                raise ValueError("File changed while taking checkpoint; retry")
            if len(data) > MAX_BYTES - total:
                raise ValueError("Workspace exceeds checkpoint limits")
            total += len(data)
            # The existing blob store intentionally rejects empty content.
            ref = store.put(data, ".bin") if data else None
            files[relative] = {"hash": hashlib.sha256(data).hexdigest(), "ref": ref, "bytes": len(data), "mode": stat.S_IMODE(after.st_mode)}
    document = {"schema_version": 1, "workspace_id": workspace.workspace_id,
                "tenant_id": workspace.tenant_id, "files": files}
    payload = json.dumps(document, sort_keys=True, ensure_ascii=False).encode()
    return {"ref": store.put(payload, ".json"), "hash": hashlib.sha256(payload).hexdigest(),
            "file_count": len(files), "bytes": total}


def load(workspace: Workspace, checkpoint: dict, store: DocArtifactStore) -> dict:
    payload = store.resolve(checkpoint["ref"]).read_bytes()
    if hashlib.sha256(payload).hexdigest() != checkpoint["hash"]:
        raise ValueError("Checkpoint manifest hash mismatch")
    document = json.loads(payload)
    if (document.get("schema_version") != 1 or document.get("workspace_id") != workspace.workspace_id
            or document.get("tenant_id") != workspace.tenant_id):
        raise ValueError("Checkpoint belongs to another workspace")
    return document


def restore_file(workspace: Workspace, document: dict, relative: str, expected_hash: str | None, store: DocArtifactStore) -> dict:
    target = safe_target(Path(workspace.path), relative)
    if target.exists() and not target.is_file():
        raise ValueError("Restore target is not a regular file")
    if file_hash(target) != expected_hash:
        raise ValueError("File version conflict; current changes were preserved")
    entry = document["files"].get(relative)
    if entry is None:
        raise ValueError("File is not present in the selected checkpoint")
    data = store.resolve(entry["ref"]).read_bytes() if entry["ref"] else b""
    if hashlib.sha256(data).hexdigest() != entry["hash"]:
        raise ValueError("Checkpoint file hash mismatch")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".restore-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, stat.S_IMODE(target.stat().st_mode) if target.exists() else entry.get("mode", 0o600))
        if file_hash(target) != expected_hash:
            raise ValueError("File version conflict; current changes were preserved")
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    return {"file": relative, "before_hash": expected_hash, "after_hash": entry["hash"]}
