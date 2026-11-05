"""Orphan-blob garbage collection — ``scripts/gc_orphan_blobs.py``.

Sweeps the document blob store (``DocArtifactStore``) for blobs no
record references any more — the inert residue a refused fenced commit
can strand — and deletes the ones older than a grace period (default
24h).  The full safety rules and the referenced-set boundary live in
:mod:`forgeflow.documents.blob_gc`; the short version: a blob is
deleted only when it is unreferenced (checked twice: once in the scan
snapshot, once again immediately before its delete) AND older than
the grace period, which is what protects commits in flight.

Operational requirements
------------------------
The referenced set is enumerated from the artifact review store, so
this must run with the deployment's own configuration (notably the
Postgres DSN environment) for the set to be the real one:

* Postgres-backed records (durable): the enumeration is the live
  referenced set and the sweep proceeds.  Under row-level security
  the enumeration runs per tenant (the tenant list comes from
  ``tenant_memberships``); a maintenance role that can see every
  tenant's rows (e.g. BYPASSRLS) is the simplest correct credential.
* In-memory records: those rows live in the *server* process's
  memory, so a fresh CLI process cannot see them and would treat
  every blob as unreferenced.  The sweep therefore REFUSES to run
  (exit 2) unless ``--allow-nondurable-refs`` is passed, accepting
  that the grace period is then the only protection.  Use that flag
  only for local/dev cleanup.

References held only in run records / tool payloads (research report
blobs, handoff payloads) are outside the enumerated set — see the
boundary discussion in :mod:`forgeflow.documents.blob_gc` and choose
the grace period accordingly.

Usage::

    python scripts/gc_orphan_blobs.py                     # sweep the configured store
    python scripts/gc_orphan_blobs.py --dry-run           # report only, delete nothing
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
    """Parse arguments and run one orphan-blob GC pass."""
    from forgeflow.documents.blob_gc import (
        DEFAULT_GRACE_SECONDS,
        ReviewStoreRefProvider,
        collect_orphan_blobs,
    )
    from forgeflow.documents.review_store import get_artifact_review_store
    from forgeflow.documents.store import DocArtifactStore

    parser = argparse.ArgumentParser(
        prog="python scripts/gc_orphan_blobs.py",
        description=(
            "Delete orphan document blobs (unreferenced and older than the "
            "grace period) from the DocArtifactStore blob root."
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
        help="minimum blob age in seconds before deletion (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be deleted, without deleting anything",
    )
    parser.add_argument(
        "--allow-nondurable-refs",
        action="store_true",
        help=(
            "run even when the referenced set is not durable (in-memory "
            "review store): the set visible to this process may be empty "
            "while the server's records reference live blobs, so the grace "
            "period is the only protection — local/dev cleanup only"
        ),
    )
    args = parser.parse_args(argv)

    blob_store = DocArtifactStore(root=args.root) if args.root else DocArtifactStore()
    if not blob_store.root.is_dir():
        print(f"[GC] blob root is not a directory: {blob_store.root}")
        return 2

    provider = ReviewStoreRefProvider(get_artifact_review_store())
    if not provider.durable and not args.allow_nondurable_refs:
        print(
            "[GC] REFUSED: the artifact review store is the in-memory "
            "backend, whose records live in the server process — this "
            "process cannot enumerate the live referenced set, so every "
            "blob would look unreferenced. Run where the Postgres "
            "backend is configured, or pass --allow-nondurable-refs "
            "for local/dev cleanup (grace period is then the only "
            "protection).",
            file=sys.stderr,
        )
        return 2
    if not provider.durable:
        print(
            "[GC] WARNING: referenced set is NOT durable (in-memory "
            "review store); proceeding under --allow-nondurable-refs. "
            "Deletions rely on the grace period alone.",
            file=sys.stderr,
        )

    report = collect_orphan_blobs(
        blob_store,
        provider,
        grace_seconds=args.grace_seconds,
        dry_run=args.dry_run,
    )
    print(
        f"[GC] root={report.root} grace_seconds={report.grace_seconds:g} dry_run={report.dry_run}"
    )
    print(
        f"[GC] scanned={report.scanned} kept_referenced={report.kept_referenced} "
        f"kept_within_grace={report.kept_within_grace}"
    )
    for ref in report.deleted:
        print(f"[GC] deleted {ref}")
    for ref in report.would_delete:
        print(f"[GC] would delete {ref}")
    for error in report.errors:
        print(f"[GC] ERROR {error}", file=sys.stderr)
    print(
        f"[GC] done: deleted={len(report.deleted)} "
        f"would_delete={len(report.would_delete)} errors={len(report.errors)}"
    )
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
