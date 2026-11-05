"""Orphan-blob garbage collection for :class:`DocArtifactStore`.

Why this exists
---------------
The fenced document-commit paths write the content-addressed blob
*before* the version row that references it commits (see
``documents/review_store.py``): an unreferenced blob is invisible, so a
refused commit strands at most an orphan.  Orphans are inert but they
accumulate; this module is the reclamation pass.

Safety rules (the whole design, in order)
-----------------------------------------
A blob is deleted only when **both** hold:

1. **Unreferenced** — its ref is absent from the referenced set (see
   the boundary below), and absent *again* on a per-candidate re-check
   performed immediately before the delete.  The re-check matters
   because ``DocArtifactStore.put`` of identical bytes returns early
   without refreshing the mtime: a blob re-referenced by a brand-new
   version row keeps its old mtime while being live again.
2. **Old** — its mtime is older than the grace period (default 24h,
   :data:`DEFAULT_GRACE_SECONDS`).  The grace period is what protects
   a blob whose fenced commit is in flight *right now*: the put lands
   seconds before the reference commits, long inside any sane grace.

Referenced-set boundary (read before running this anywhere)
-----------------------------------------------------------
The provider shipped here, :class:`ReviewStoreRefProvider`, enumerates
the ``content_ref`` of every version row in the artifact review store
— the durable record for document/text/sheet/pdf deliverables.
References held *only* in run records and tool payloads (research
report blobs, handoff payloads, …) are **not** enumerable by this
pass: for those blobs the grace period is the only protection, so an
operator sweeping a root that holds them should choose a grace period
longer than the retention of the records that name them.  Blobs under
a *different* root (e.g. codeplane ``_snapshots`` stores) are likewise
out of scope unless the operator points the collector at that root
with a provider that knows its records.

Only files matching the content-addressed layout
(``<2-hex shard>/<64-hex name><suffix>``) under the blob root are
candidates at all.  ``.tmp-*`` files are in-flight writes and are never
touched; anything else in the tree is ignored.  Deletion is
race-tolerant: a blob that vanishes between scan and delete (a peer
collector, a commit's own discard) is not an error.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Protocol

from forgeflow.documents.review_store import (
    ArtifactReviewStore,
    PostgresArtifactReviewStore,
)
from forgeflow.documents.store import DocArtifactStore

__all__ = [
    "DEFAULT_GRACE_SECONDS",
    "OrphanBlobReport",
    "RefProvider",
    "ReviewStoreRefProvider",
    "collect_orphan_blobs",
]

logger = logging.getLogger(__name__)

#: Default grace period: a blob younger than this is never deleted,
#: referenced or not.  In-flight fenced commits complete in seconds;
#: 24h leaves orders of magnitude of headroom.
DEFAULT_GRACE_SECONDS = 24.0 * 60 * 60

_SHARD_RE = re.compile(r"^[0-9a-f]{2}$")
_BLOB_NAME_RE = re.compile(r"^[0-9a-f]{64}\..+$")


class RefProvider(Protocol):
    """Source of the referenced set for one GC pass."""

    def refs(self) -> set[str]:
        """Snapshot of every currently referenced blob ref."""
        ...

    def contains(self, ref: str) -> bool:
        """Fresh single-ref check, used as the pre-delete re-check."""
        ...


class ReviewStoreRefProvider:
    """Referenced set enumerated from an :class:`ArtifactReviewStore`.

    ``durable`` is True only for the Postgres backend: its rows
    survive process restarts, so a *separate* maintenance process can
    see the live referenced set.  The in-memory backend's rows are
    process-lifetime — a fresh CLI process enumerates an empty store
    and would see every blob as unreferenced — so the CLI refuses to
    run on it without an explicit operator override.
    """

    def __init__(self, store: ArtifactReviewStore) -> None:
        self._store = store

    @property
    def durable(self) -> bool:
        return isinstance(self._store, PostgresArtifactReviewStore)

    def refs(self) -> set[str]:
        return self._store.referenced_content_refs()

    def contains(self, ref: str) -> bool:
        return self._store.has_content_ref(ref)


@dataclass
class OrphanBlobReport:
    """Outcome of one GC pass (counts plus the exact refs acted on)."""

    root: str
    grace_seconds: float
    dry_run: bool
    scanned: int = 0
    deleted: list[str] = field(default_factory=list)
    would_delete: list[str] = field(default_factory=list)
    kept_referenced: int = 0
    kept_within_grace: int = 0
    errors: list[str] = field(default_factory=list)


def _normalise_provider(
    refs: RefProvider | Callable[[], set[str]] | Iterable[str],
) -> tuple[Callable[[], set[str]], Callable[[str], bool]]:
    """Adapt the accepted provider shapes to (snapshot, contains).

    A plain iterable is a static snapshot (the re-check then consults
    the same set — no fresh information, which the caller accepts by
    choosing that shape).  A callable is re-invoked for the re-check;
    an object with ``refs()`` uses its ``contains()`` when it has one.
    """
    refs_fn = getattr(refs, "refs", None)
    if callable(refs_fn):
        contains_fn = getattr(refs, "contains", None)
        if callable(contains_fn):
            return refs_fn, contains_fn
        return refs_fn, lambda ref: ref in refs_fn()
    if callable(refs):
        return refs, lambda ref: ref in refs()
    frozen = set(refs)
    return (lambda: set(frozen)), frozen.__contains__


def _iter_blob_candidates(blob_store: DocArtifactStore) -> Iterable[tuple[str, float]]:
    """Yield ``(ref, mtime)`` for every content-addressed blob file.

    Only the two-level shard layout is walked, and only names matching
    the content-addressed scheme are candidates — ``.tmp-*`` in-flight
    writes and any foreign file are never yielded.
    """
    root = blob_store.root
    for shard in sorted(root.iterdir()):
        if not shard.is_dir() or not _SHARD_RE.match(shard.name):
            continue
        for entry in sorted(shard.iterdir()):
            if not entry.is_file() or entry.name.startswith(".tmp-"):
                continue
            if not _BLOB_NAME_RE.match(entry.name):
                continue
            yield f"{shard.name}/{entry.name}", entry.stat().st_mtime


def collect_orphan_blobs(
    blob_store: DocArtifactStore,
    refs: RefProvider | Callable[[], set[str]] | Iterable[str],
    *,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
    now: float | None = None,
    dry_run: bool = False,
) -> OrphanBlobReport:
    """Delete orphan blobs under ``blob_store``'s root.

    Args:
        blob_store: the store whose root is swept (its ``delete`` is
            the removal primitive, so containment rules are the
            store's own).
        refs: the referenced set — a :class:`RefProvider`, a callable
            returning the current set of referenced refs, or a static
            iterable of refs.
        grace_seconds: minimum blob age before an unreferenced blob
            may be deleted.
        now: epoch seconds to measure ages against (injectable for
            tests); defaults to the current time.
        dry_run: report what would be deleted without deleting.

    Returns:
        An :class:`OrphanBlobReport`.  Per-candidate failures land in
        ``report.errors`` and never abort the pass.
    """
    snapshot_refs, contains_ref = _normalise_provider(refs)
    report = OrphanBlobReport(
        root=str(blob_store.root),
        grace_seconds=float(grace_seconds),
        dry_run=dry_run,
    )
    if not blob_store.root.is_dir():
        report.errors.append(f"blob root does not exist: {blob_store.root}")
        return report
    moment = time.time() if now is None else float(now)
    cutoff = moment - float(grace_seconds)
    referenced = snapshot_refs()
    for ref, mtime in _iter_blob_candidates(blob_store):
        report.scanned += 1
        if ref in referenced:
            report.kept_referenced += 1
            continue
        if mtime >= cutoff:
            report.kept_within_grace += 1
            continue
        # Deletion candidate.  Re-check against a FRESH lookup before
        # deleting: the blob may have become referenced since the
        # snapshot (a new version row naming the same bytes — whose
        # put did not refresh the old mtime).
        try:
            if contains_ref(ref):
                report.kept_referenced += 1
                continue
        except Exception as exc:  # noqa: BLE001 — a failed re-check must not delete
            report.errors.append(f"{ref}: reference re-check failed: {exc}")
            continue
        if dry_run:
            report.would_delete.append(ref)
            continue
        try:
            removed = blob_store.delete(ref)
        except Exception as exc:  # noqa: BLE001 — one bad blob must not abort the pass
            report.errors.append(f"{ref}: delete failed: {exc}")
            continue
        if removed:
            report.deleted.append(ref)
        # removed=False: a peer collector or a commit's discard got
        # there first — the desired end state, not an error.
    logger.info(
        "orphan blob GC | root=%s scanned=%d deleted=%d would_delete=%d "
        "kept_referenced=%d kept_within_grace=%d errors=%d dry_run=%s",
        report.root,
        report.scanned,
        len(report.deleted),
        len(report.would_delete),
        report.kept_referenced,
        report.kept_within_grace,
        len(report.errors),
        report.dry_run,
    )
    return report
