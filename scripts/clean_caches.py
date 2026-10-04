"""Build/test cache cleaner — ``scripts/clean_caches.py``.

Removes **only** directories that are unambiguously build or test caches:

  * ``__pycache__``       (CPython bytecode)
  * ``.pytest_cache``     (pytest)
  * ``.mypy_cache``       (mypy)
  * ``.ruff_cache``       (ruff)

Anything that could be stateful or expensive to rebuild is left strictly alone:
``.env``, ``.testrelic``, databases and data directories, uploaded files,
knowledge bases, Ollama models, ``.git``, ``node_modules`` and every config
file. The recursive walk **skips** ``.git``, ``node_modules``, ``.venv`` and
``venv`` entirely, so it never descends into a VCS store or a virtualenv.

The implementation uses :mod:`pathlib` + :func:`shutil.rmtree` (no ``rm -rf``,
no shell, no wildcards) and therefore behaves identically on Windows and Linux.

Usage::

    python scripts/clean_caches.py               # delete caches under the repo root
    python scripts/clean_caches.py --dry-run      # report only, delete nothing
    python scripts/clean_caches.py --root <dir>   # choose the starting directory
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

#: Directory basenames that are safe to delete wholesale.
CACHE_DIR_NAMES: frozenset[str] = frozenset(
    {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)

#: Directory basenames never descended into (VCS stores, virtualenvs, deps).
SKIP_DIR_NAMES: frozenset[str] = frozenset({".git", "node_modules", ".venv", "venv"})

#: Default scan root: the repository root, i.e. the parent of scripts/.
_REPO_ROOT = Path(__file__).resolve().parent.parent


def iter_cache_dirs(root: Path) -> list[Path]:
    """Return every cache directory beneath ``root``.

    The walk prunes :data:`SKIP_DIR_NAMES` (so a nested ``.git`` or
    ``node_modules`` is never entered) and does not descend into a cache
    directory once found.

    Args:
        root: Directory to scan.

    Returns:
        Cache directory paths, in traversal order.
    """
    found: list[Path] = []
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            # Unreadable directory (permissions, race) — skip it, keep going.
            continue
        for entry in entries:
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            if not is_dir:
                continue
            if entry.name in SKIP_DIR_NAMES:
                continue
            if entry.name in CACHE_DIR_NAMES:
                found.append(entry)
                continue
            stack.append(entry)
    return found


def _relative(path: Path, root: Path) -> str:
    """Render ``path`` relative to ``root`` when possible, else absolute."""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and delete (or, in dry-run, report) the caches."""
    parser = argparse.ArgumentParser(
        prog="python scripts/clean_caches.py",
        description=(
            "Delete only build/test caches (__pycache__, .pytest_cache, .mypy_cache, .ruff_cache)."
        ),
    )
    parser.add_argument(
        "--root",
        default=str(_REPO_ROOT),
        help="directory to scan (default: the repository root)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the caches that would be removed, without deleting them",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"[CLEAN] root is not a directory: {root}")
        return 2

    caches = iter_cache_dirs(root)
    if not caches:
        print(f"[CLEAN] no build/test caches found under {root}")
        return 0

    removed = 0
    for cache in caches:
        rel = _relative(cache, root)
        if args.dry_run:
            print(f"[CLEAN] would remove {rel}")
            continue
        try:
            shutil.rmtree(cache)
        except OSError as exc:
            print(f"[CLEAN] FAILED  {rel} ({type(exc).__name__}: {exc})")
            continue
        removed += 1
        print(f"[CLEAN] removed {rel}")

    if args.dry_run:
        print(f"[CLEAN] dry-run: {len(caches)} cache dir(s) would be removed")
    else:
        print(f"[CLEAN] done: removed {removed} of {len(caches)} cache dir(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
