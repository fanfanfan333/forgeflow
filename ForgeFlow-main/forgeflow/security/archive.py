"""Validate an entire untrusted ZIP before writing inside an execution workspace."""

import shutil
import stat
import zipfile
from pathlib import Path, PureWindowsPath


def extract_zip(
    archive: zipfile.ZipFile,
    target: Path,
    *,
    max_bytes: int = 200 * 1024 * 1024,
    max_files: int = 10000,
) -> None:
    root = target.resolve()
    entries = archive.infolist()
    if len(entries) > max_files or sum(entry.file_size for entry in entries) > max_bytes:
        raise ValueError("ZIP exceeds workspace extraction limits")
    destinations = []
    seen = set()
    for entry in entries:
        name = entry.filename.replace("\\", "/")
        parts = name.split("/")
        if PureWindowsPath(name).drive or name.startswith("/") or ".." in parts or ":" in name:
            raise ValueError("ZIP contains an unsafe path")
        if stat.S_ISLNK(entry.external_attr >> 16):
            raise ValueError("ZIP symbolic links are not permitted")
        destination = (root / name).resolve()
        if not destination.is_relative_to(root):
            raise ValueError("ZIP path escapes workspace")
        key = str(destination).casefold()
        if key in seen:
            raise ValueError("ZIP contains duplicate destination paths")
        seen.add(key)
        destinations.append((entry, destination))
    for entry, destination in destinations:
        if entry.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as source, destination.open("xb") as output:
                shutil.copyfileobj(source, output, length=65536)
