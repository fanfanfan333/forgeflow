"""Orphan-blob REPORT — ``scripts/gc_orphan_blobs.py``.

Scans the document blob store (``DocArtifactStore``) for blobs no
enumerated record references any more — the inert residue a refused
fenced commit can strand — and LISTS the ones older than a grace
period (default 24h), with their sizes.  Despite the historical file
name, this tool deletes NOTHING and has no deletion switch.

Why report-only
---------------
The referenced set is enumerated from the artifact review store, and
that enumeration is INCOMPLETE: references held only in run records
and tool payloads (research report blobs, handoff payloads, …) cannot
be enumerated by a maintenance pass, so a listed candidate may still
be in use.  Automated deletion on an incomplete set is unsafe; a real
reference-counting garbage collector is future work.  See
:mod:`forgeflow.documents.blob_gc` for the full discussion.

Operational notes
-----------------
This must run with the deployment's own configuration (notably the
Postgres DSN environment) for the referenced set to be the real one:

* Postgres-backed records (durable): the enumeration is the live
  referenced set.  Under row-level security the enumeration runs per
  tenant (the tenant list comes from ``tenant_memberships``); if that
  registry cannot be read, the enumeration RAISES and this tool fails
  with a non-zero exit rather than printing a candidate list computed
  from a partial set.
* In-memory records: those rows live in the *server* process's
  memory, so a fresh CLI process cannot see them and treats every
  blob as unreferenced.  The tool prints a WARNING in that case: the
  candidate list is an upper bound, not a deletion list (nothing is
  deleted either way).

Exit codes: 0 on a clean report; 1 on a real error (enumeration
failure, missing blob root, …).  There is no exit path that deletes.

Usage::

    python scripts/gc_orphan_blobs.py                     # report on the configured store
    python scripts/gc_orphan_blobs.py --grace-seconds 86400
    python scripts/gc_orphan_blobs.py --root <base-dir>   # DocArtifactStore base dir
                                                          # ("documents" is appended,
                                                          #  exactly as DocArtifactStore)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

#: Repository root (parent of scripts/) — makes ``forgeflow`` importable
#: when the script is invoked from another working directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and print one orphan-blob report (read-only)."""
    from forgeflow.documents.blob_gc import (
        DEFAULT_GRACE_SECONDS,
        ReviewStoreRefProvider,
        report_orphan_blobs,
    )
    from forgeflow.documents.review_store import get_artifact_review_store
    from forgeflow.documents.store import DocArtifactStore

    parser = argparse.ArgumentParser(
        prog="python scripts/gc_orphan_blobs.py",
        description=(
            "Report orphan document blobs (unreferenced in the enumerated "
            "record set and older than the grace period) in the "
            "DocArtifactStore blob root. READ-ONLY: this tool deletes "
            "nothing — reference enumeration is incomplete (refs held "
            "only in run records / tool payloads are not enumerable), so "
            "candidates may include blobs still in use. A real "
            "reference-counting GC is future work."
        ),
    )
    parser.add_argument(
        "--root",
        default=None,
        help=(
            "DocArtifactStore base directory (the 'documents' subdir is "
            "appended, as DocArtifactStore interprets it); default: the "
            "configured resource store root"
        ),
    )
    parser.add_argument(
        "--grace-seconds",
        type=float,
        default=DEFAULT_GRACE_SECONDS,
        help="minimum blob age in seconds before listing (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    blob_store = DocArtifactStore(root=args.root) if args.root else DocArtifactStore()

    provider = ReviewStoreRefProvider(get_artifact_review_store())
    if not provider.durable:
        print(
            "[GC] WARNING: the artifact review store is the in-memory "
            "backend, whose records live in the server process — this "
            "process enumerates an empty referenced set, so every blob "
            "older than the grace period will be listed. The candidate "
            "list is an UPPER BOUND, not a deletion list. Run where the "
            "Postgres backend is configured for the live referenced set.",
            file=sys.stderr,
        )

    try:
        report = report_orphan_blobs(
            blob_store,
            provider,
            grace_seconds=args.grace_seconds,
        )
    except Exception as exc:  # noqa: BLE001 — fail visibly, print no candidate list
        print(f"[GC] ERROR: referenced-set enumeration failed: {exc}", file=sys.stderr)
        return 1
    print("[GC] orphan blob report — READ-ONLY, nothing is deleted")
    print(
        "[GC] note: reference enumeration is incomplete (refs held only in "
        "run records / tool payloads are not enumerable); candidates may "
        "include blobs still in use"
    )
    print(f"[GC] root={report.root} grace_seconds={report.grace_seconds:g}")
    print(
        f"[GC] scanned={report.scanned} kept_referenced={report.kept_referenced} "
        f"kept_within_grace={report.kept_within_grace}"
    )
    for candidate in report.candidates:
        print(
            f"[GC] candidate {candidate.ref} size={candidate.size_bytes} "
            f"age_seconds={candidate.age_seconds:.0f}"
        )
    for error in report.errors:
        print(f"[GC] ERROR {error}", file=sys.stderr)
    print(
        f"[GC] done: candidates={len(report.candidates)} "
        f"candidate_bytes={report.candidate_bytes} errors={len(report.errors)}"
    )
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
