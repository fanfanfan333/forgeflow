"""Prevent two API processes from dispatching against the same local store."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def api_instance_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / ".api-instance.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"1")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("another API process is using this resource store") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("another API process is using this resource store") from exc
        yield
    finally:
        handle.close()
