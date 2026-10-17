"""QA freeze/snapshot discipline — prove no source changed during a full run.

The project has no git, so a "snapshot" is a manifest of the working tree:
for each file under the watched roots we record mtime_ns + size + sha256.
Two manifests taken around a full test run must be identical; any difference
means the numbers from that run are not reproducible and must be discarded.

Usage:
    python qa_tmp/mtime_snapshot.py --label freeze --out qa_tmp/snapshot_freeze.json
    #   ... run the full suite ...
    python qa_tmp/mtime_snapshot.py --label after  --out qa_tmp/snapshot_after.json
    python qa_tmp/mtime_snapshot.py --compare qa_tmp/snapshot_freeze.json qa_tmp/snapshot_after.json
Exit code: 0 when clean (or when writing a snapshot), 1 when --compare finds drift.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WATCH = ("forgeflow", "tests", "frontend/src", "alembic")
SKIP_DIRS = {"__pycache__", ".pytest_cache", "node_modules", ".mypy_cache", ".ruff_cache"}
SKIP_SUFFIX = {".pyc", ".pyo"}


def _iter_files(root: Path):
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix in SKIP_SUFFIX or p.name.endswith(".pyc"):
            continue
        yield p


def _sha256(path: Path, limit: int = 2_000_000) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        data = fh.read(limit)
    h.update(data)
    return h.hexdigest()


def make_manifest() -> dict:
    files: dict[str, dict] = {}
    for rel in WATCH:
        base = ROOT / rel
        if not base.exists():
            continue
        for p in _iter_files(base):
            key = str(p.relative_to(ROOT)).replace("\\", "/")
            st = p.stat()
            files[key] = {"mtime_ns": st.st_mtime_ns, "size": st.st_size, "sha256": _sha256(p)}
    return {"root": str(ROOT), "watch": list(WATCH), "file_count": len(files), "files": files}


def compare(a: dict, b: dict) -> dict:
    fa, fb = a.get("files", {}), b.get("files", {})
    added = sorted(set(fb) - set(fa))
    removed = sorted(set(fa) - set(fb))
    changed = sorted(
        k for k in (set(fa) & set(fb))
        if (fa[k]["mtime_ns"], fa[k]["sha256"]) != (fb[k]["mtime_ns"], fb[k]["sha256"])
    )
    return {
        "clean": not (added or removed or changed),
        "added": added,
        "removed": removed,
        "changed": changed,
        "counts": {"added": len(added), "removed": len(removed), "changed": len(changed)},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="snapshot")
    ap.add_argument("--out")
    ap.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"))
    args = ap.parse_args()

    if args.compare:
        before = json.loads(Path(args.compare[0]).read_text(encoding="utf-8"))
        after = json.loads(Path(args.compare[1]).read_text(encoding="utf-8"))
        diff = compare(before, after)
        print(json.dumps(diff, indent=2, ensure_ascii=False))
        print("SNAPSHOT_CLEAN" if diff["clean"] else "SNAPSHOT_DRIFT_DETECTED")
        return 0 if diff["clean"] else 1

    manifest = make_manifest()
    manifest["label"] = args.label
    out = Path(args.out) if args.out else (ROOT / "qa_tmp" / f"snapshot_{args.label}.json")
    out.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"SNAPSHOT_WRITTEN {out} files={manifest['file_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
