"""Orphan-blob REPORT for :class:`DocArtifactStore` — read-only.

Why this exists
---------------
The fenced document-commit paths write the content-addressed blob
*before* the version row that references it commits (see
``documents/review_store.py``): an unreferenced blob is invisible, so a
refused commit strands at most an orphan.  Orphans are inert but they
accumulate; this module gives operators visibility into them.

Why this tool REPORTS ONLY and deletes nothing
----------------------------------------------
A candidate list is only as safe as the referenced set behind it, and
that set is **incomplete by construction**: the provider shipped here,
:class:`ReviewStoreRefProvider`, enumerates the ``content_ref`` of
every version row in the artifact review store — the durable record
for document/text/sheet/pdf deliverables — but references held *only*
in run records and tool payloads (research report blobs, handoff
payloads, …) are **not** enumerable by a synchronous maintenance pass.
A blob named only by such a record is indistinguishable from a true
orphan, so automated deletion on this enumeration could delete blobs
that are still in use.  This tool therefore never deletes: it lists
candidates (with sizes, so an operator can judge the reclaimable
total) and stops there.  A real reference-counting garbage collector
— one whose referenced set is complete by design — is future work,
deliberately out of scope here.

What counts as a candidate
--------------------------
A blob is listed as a candidate only when **both** hold:

1. **Unreferenced** — its ref is absent from the enumerated
   referenced set (see the boundary above: absence is evidence, not
   proof, which is exactly why nothing is deleted).
2. **Old** — its mtime is older than the grace period (default 24h,
   :data:`DEFAULT_GRACE_SECONDS`).  The grace period keeps a blob
   whose fenced commit is in flight *right now* (the put lands
   seconds before the reference commits) off the list entirely.

Only files matching the content-addressed layout
(``<2-hex shard>/<64-hex name><suffix>``) under the blob root are
considered at all.  ``.tmp-*`` files are in-flight writes and are
never listed; anything else in the tree is ignored.  Blobs under a
*different* root (e.g. codeplane ``_snapshots`` stores) are out of
scope unless the operator points the report at that root with a
provider that knows its records.

Enumeration failure is loud: if the referenced set cannot be
enumerated completely (e.g. the tenant registry is unreadable), the
report raises instead of returning a candidate list computed from a
partial set presented as whole.
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
    "OrphanBlobCandidate",
    "OrphanBlobReport",
    "RefProvider",
    "ReviewStoreRefProvider",
    "report_orphan_blobs",
]

logger = logging.getLogger(__name__)

#: Default grace period: a blob younger than this is never listed as
#: a candidate, referenced or not.  In-flight fenced commits complete
#: in seconds; 24h leaves orders of magnitude of headroom.
DEFAULT_GRACE_SECONDS = 24.0 * 60 * 60

_SHARD_RE = re.compile(r"^[0-9a-f]{2}$")
_BLOB_NAME_RE = re.compile(r"^[0-9a-f]{64}\..+$")


class RefProvider(Protocol):
    """Source of the referenced set for one report pass."""

    def refs(self) -> set[str]:
        """Snapshot of every currently referenced blob ref."""
        ...


class ReviewStoreRefProvider:
    """Referenced set enumerated from an :class:`ArtifactReviewStore`.

    ``durable`` is True only for the Postgres backend: its rows
    survive process restarts, so a *separate* maintenance process can
    see the live referenced set.  The in-memory backend's rows are
    process-lifetime — a fresh CLI process enumerates an empty store
    and sees every blob as unreferenced — so the CLI prints a warning
    in that case: the candidate list is then an upper bound computed
    from an empty set, not a statement that the blobs are unused.
    (Nothing is ever deleted on the strength of this list either way;
    see the module docstring.)
    """

    def __init__(self, store: ArtifactReviewStore) -> None:
        self._store = store

    @property
    def durable(self) -> bool:
        return isinstance(self._store, PostgresArtifactReviewStore)

    def refs(self) -> set[str]:
        return self._store.referenced_content_refs()


@dataclass
class OrphanBlobCandidate:
    """One blob the report lists as an orphan candidate."""

    ref: str
    size_bytes: int
    mtime: float  # epoch seconds of the blob's last modification
    age_seconds: float  # age at report time (``now - mtime``)


@dataclass
class OrphanBlobReport:
    """Outcome of one report pass.  Read-only: nothing was deleted."""

    root: str
    grace_seconds: float
    scanned: int = 0
    candidates: list[OrphanBlobCandidate] = field(default_factory=list)
    kept_referenced: int = 0
    kept_within_grace: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def candidate_bytes(self) -> int:
        """Total size of the listed candidates, in bytes."""
        return sum(c.size_bytes for c in self.candidates)


def _snapshot_refs(
    refs: RefProvider | Callable[[], set[str]] | Iterable[str],
) -> Callable[[], set[str]]:
    """Adapt the accepted provider shapes to a snapshot callable.

    An object with ``refs()`` is used as-is; a plain callable is
    invoked; a static iterable is frozen into a set.
    """
    refs_fn = getattr(refs, "refs", None)
    if callable(refs_fn):
        return refs_fn
    if callable(refs):
        return refs
    frozen = set(refs)
    return lambda: set(frozen)


def _iter_blob_candidates(
    blob_store: DocArtifactStore,
) -> Iterable[tuple[str, float, int]]:
    """Yield ``(ref, mtime, size_bytes)`` for every content-addressed blob.

    Only the two-level shard layout is walked, and only names matching
    the content-addressed scheme are yielded — ``.tmp-*`` in-flight
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
            stat = entry.stat()
            yield f"{shard.name}/{entry.name}", stat.st_mtime, stat.st_size


def report_orphan_blobs(
    blob_store: DocArtifactStore,
    refs: RefProvider | Callable[[], set[str]] | Iterable[str],
    *,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
    now: float | None = None,
) -> OrphanBlobReport:
    """List orphan-blob candidates under ``blob_store``'s root.

    READ-ONLY: this function never deletes or modifies anything.  A
    blob is listed when it is absent from the referenced set *and*
    older than ``grace_seconds`` — but the referenced set is
    incomplete (see the module docstring), so a listed candidate may
    still be in use; the list is operator input, not a deletion
    instruction.

    Args:
        blob_store: the store whose root is scanned.
        refs: the referenced set — a :class:`RefProvider`, a callable
            returning the current set of referenced refs, or a static
            iterable of refs.
        grace_seconds: minimum blob age before an unreferenced blob
            is listed as a candidate.
        now: epoch seconds to measure ages against (injectable for
            tests); defaults to the current time.

    Returns:
        An :class:`OrphanBlobReport`.

    Raises:
        Whatever the referenced-set enumeration raises.  A partial
        set must never be reported as complete, so enumeration
        failure aborts the report instead of landing in
        ``report.errors``.
    """
    snapshot_refs = _snapshot_refs(refs)
    report = OrphanBlobReport(
        root=str(blob_store.root),
        grace_seconds=float(grace_seconds),
    )
    if not blob_store.root.is_dir():
        report.errors.append(f"blob root does not exist: {blob_store.root}")
        return report
    moment = time.time() if now is None else float(now)
    cutoff = moment - float(grace_seconds)
    referenced = snapshot_refs()
    for ref, mtime, size_bytes in _iter_blob_candidates(blob_store):
        report.scanned += 1
        if ref in referenced:
            report.kept_referenced += 1
            continue
        if mtime >= cutoff:
            report.kept_within_grace += 1
            continue
        report.candidates.append(
            OrphanBlobCandidate(
                ref=ref,
                size_bytes=size_bytes,
                mtime=mtime,
                age_seconds=moment - mtime,
            )
        )
    logger.info(
        "orphan blob report | root=%s scanned=%d candidates=%d "
        "candidate_bytes=%d kept_referenced=%d kept_within_grace=%d errors=%d",
        report.root,
        report.scanned,
        len(report.candidates),
        report.candidate_bytes,
        report.kept_referenced,
        report.kept_within_grace,
        len(report.errors),
    )
    return report
