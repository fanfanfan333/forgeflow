"""Final wrap-up — blob durability (directory fsync) + orphan blob GC.

Item 1: ``DocArtifactStore.put`` fsyncs the file but (before the fix)
never the directory — a rename is not durable until its containing
directory is fsynced, and a newly created directory entry is not
durable until its parent is.  The tests record os.open/os.fsync/
os.replace and assert the directory fds are fsynced, in order.

Item 2: ``forgeflow.documents.blob_gc.collect_orphan_blobs`` deletes a
blob only when it is unreferenced (snapshot + a fresh per-candidate
re-check) AND older than the grace period.  Seeded through the real
stores; mtimes are pinned with ``os.utime`` against an injected
``now`` — no sleeps.

Both items fail on the pre-fix head: item 1 behaviorally (only the
file fd is ever fsynced), item 2 at import (the module, the review
stores' referenced-set enumeration, and the CLI do not exist).
"""

from __future__ import annotations

import importlib.util
import os
import time
from pathlib import Path

import pytest

from forgeflow.documents.blob_gc import (
    ReviewStoreRefProvider,
    collect_orphan_blobs,
)
from forgeflow.documents.review_store import (
    InMemoryArtifactReviewStore,
    reset_artifact_review_store,
    set_artifact_review_store,
)
from forgeflow.documents.store import DocArtifactStore

_REPO_ROOT = Path(__file__).resolve().parents[2]

NOW = 1_800_000_000.0  # fixed epoch the GC ages are measured against
GRACE = 24.0 * 3600


# --------------------------------------------------------------------------- #
# Item 1 — directory fsync                                                    #
# --------------------------------------------------------------------------- #
class _IoRecorder:
    """Recording wrappers around os.open / os.fsync / os.replace."""

    def __init__(self) -> None:
        self.events: list[tuple] = []
        self._real_open = os.open
        self._real_fsync = os.fsync
        self._real_replace = os.replace

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(os, "open", self.open)
        monkeypatch.setattr(os, "fsync", self.fsync)
        monkeypatch.setattr(os, "replace", self.replace)

    def open(self, path, flags, *args, **kwargs):  # noqa: ANN001, ANN202
        fd = self._real_open(path, flags, *args, **kwargs)
        self.events.append(("open", os.fspath(path), fd))
        return fd

    def fsync(self, fd):  # noqa: ANN001, ANN202
        self.events.append(("fsync", fd))
        return self._real_fsync(fd)

    def replace(self, src, dst):  # noqa: ANN001, ANN202
        self.events.append(("replace", os.fspath(src), os.fspath(dst)))
        return self._real_replace(src, dst)

    def fsync_timeline(self) -> list[tuple[int, str]]:
        """(event index, path) for every fsync, resolving fds in order."""
        fd_paths: dict[int, str] = {}
        timeline: list[tuple[int, str]] = []
        for idx, event in enumerate(self.events):
            if event[0] == "open":
                fd_paths[event[2]] = event[1]
            elif event[0] == "fsync":
                timeline.append((idx, fd_paths.get(event[1], f"<fd {event[1]}>")))
        return timeline

    def replace_index(self) -> int:
        return next(i for i, e in enumerate(self.events) if e[0] == "replace")


def test_put_fsyncs_containing_directory_after_rename(tmp_path, monkeypatch):
    store = DocArtifactStore(root=tmp_path / "base")
    content = b"durable-bytes"
    ref = DocArtifactStore.ref_for(content)
    target = store.root / ref
    target.parent.mkdir(parents=True, exist_ok=True)  # nothing to create during put

    recorder = _IoRecorder()
    recorder.install(monkeypatch)
    assert store.put(content) == ref

    timeline = recorder.fsync_timeline()
    fsynced = [path for _, path in timeline]
    # The temp FILE is still fsynced before the rename (pre-existing)…
    assert any(p.startswith(str(target.parent)) and p != str(target.parent) for p in fsynced)
    # …and the containing DIRECTORY is fsynced after the rename.
    dir_fsyncs = [idx for idx, path in timeline if path == str(target.parent)]
    assert dir_fsyncs, f"directory never fsynced; fsyncs were: {timeline}"
    assert min(dir_fsyncs) > recorder.replace_index()
    # Behaviour unchanged: same ref, bytes round-trip.
    assert store.get(ref) == content


def test_put_fsyncs_parents_of_newly_created_directories(tmp_path, monkeypatch):
    base = tmp_path / "deep" / "base"  # neither level exists yet
    store = DocArtifactStore(root=base)
    recorder = _IoRecorder()
    recorder.install(monkeypatch)

    ref = store.put(b"fresh-tree-bytes")

    fsynced_dirs = {path for _, path in recorder.fsync_timeline() if Path(path).is_dir()}
    # The shard dir was created → its parent (the documents root) is fsynced.
    assert str(store.root) in fsynced_dirs
    # The documents root was created → its parent (the base) is fsynced.
    assert str(base) in fsynced_dirs
    assert store.get(ref) == b"fresh-tree-bytes"


def test_put_still_works_when_directories_cannot_be_opened(tmp_path, monkeypatch):
    """Control: on platforms where a directory cannot be opened
    (Windows), the fsync helper must skip — put must not break."""
    store = DocArtifactStore(root=tmp_path / "base")
    real_open = os.open

    def guarded_open(path, flags, *args, **kwargs):  # noqa: ANN001, ANN202
        if os.path.isdir(path):
            raise PermissionError("directories cannot be opened here")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded_open)
    ref = store.put(b"portable-bytes")
    assert store.get(ref) == b"portable-bytes"


# --------------------------------------------------------------------------- #
# Item 2 — orphan blob GC                                                     #
# --------------------------------------------------------------------------- #
def _age(store: DocArtifactStore, ref: str, mtime: float) -> None:
    path = store.root / ref
    os.utime(path, (mtime, mtime))


def test_gc_deletes_unreferenced_blob_older_than_grace(tmp_path):
    store = DocArtifactStore(root=tmp_path / "base")
    ref = store.put(b"orphan-bytes", suffix=".bin")
    _age(store, ref, NOW - 48 * 3600)

    report = collect_orphan_blobs(store, set(), grace_seconds=GRACE, now=NOW)

    assert report.deleted == [ref]
    assert report.scanned == 1
    assert report.errors == []
    assert not (store.root / ref).exists()


def test_gc_keeps_unreferenced_blob_within_grace(tmp_path):
    store = DocArtifactStore(root=tmp_path / "base")
    ref = store.put(b"fresh-orphan", suffix=".bin")
    _age(store, ref, NOW - 3600)  # one hour old, grace is 24h

    report = collect_orphan_blobs(store, set(), grace_seconds=GRACE, now=NOW)

    assert report.deleted == []
    assert report.kept_within_grace == 1
    assert (store.root / ref).exists()


def test_gc_keeps_referenced_blob_and_shared_ref_until_last_record_goes(tmp_path):
    store = DocArtifactStore(root=tmp_path / "base")
    review = InMemoryArtifactReviewStore(blob_store=store)
    v1, _ = review.create_pending_version("t-1", artifact_id="art-1", content=b"shared-content")
    v2, _ = review.create_pending_version("t-1", artifact_id="art-2", content=b"shared-content")
    ref = v1.content_ref
    assert ref is not None and v2.content_ref == ref  # identical bytes share the ref
    _age(store, ref, NOW - 72 * 3600)
    provider = ReviewStoreRefProvider(review)

    report = collect_orphan_blobs(store, provider, grace_seconds=GRACE, now=NOW)
    assert report.deleted == []
    assert report.kept_referenced == 1
    assert (store.root / ref).exists()

    # Remove ONE of the two referencing rows — still referenced.
    key1 = next(k for k in review._versions if k[1] == "art-1")
    del review._versions[key1]
    report = collect_orphan_blobs(store, provider, grace_seconds=GRACE, now=NOW)
    assert report.deleted == []
    assert (store.root / ref).exists()

    # Remove the last referencing row — now it is a true orphan.
    key2 = next(k for k in review._versions if k[1] == "art-2")
    del review._versions[key2]
    report = collect_orphan_blobs(store, provider, grace_seconds=GRACE, now=NOW)
    assert report.deleted == [ref]
    assert not (store.root / ref).exists()


def test_gc_never_touches_tmp_or_non_scheme_files(tmp_path):
    store = DocArtifactStore(root=tmp_path / "base")
    control = store.put(b"control-blob", suffix=".bin")
    _age(store, control, NOW - 3600)  # within grace — the only real candidate

    shard_dir = store.root / control.split("/")[0]
    tmp_file = shard_dir / ".tmp-stale-write"
    tmp_file.write_bytes(b"partial")
    foreign = store.root / "README.txt"
    foreign.write_bytes(b"not a blob")
    bad_shard = store.root / "zz"
    bad_shard.mkdir()
    bad_shard_file = bad_shard / ("ab" * 32 + ".bin")
    bad_shard_file.write_bytes(b"wrong shard")
    bad_name = shard_dir / "short.txt"
    bad_name.write_bytes(b"wrong name")
    for path in (tmp_file, foreign, bad_shard_file, bad_name):
        old = NOW - 96 * 3600
        os.utime(path, (old, old))

    report = collect_orphan_blobs(store, set(), grace_seconds=GRACE, now=NOW)

    assert report.scanned == 1  # only the scheme-conforming blob was a candidate
    assert report.deleted == []
    for path in (tmp_file, foreign, bad_shard_file, bad_name):
        assert path.exists(), f"GC touched a non-candidate: {path}"


def test_gc_recheck_saves_blob_that_became_referenced_after_scan(tmp_path):
    store = DocArtifactStore(root=tmp_path / "base")
    ref = store.put(b"re-referenced", suffix=".bin")
    _age(store, ref, NOW - 48 * 3600)

    class _FlipProvider:
        """Snapshot says unreferenced; the fresh re-check says referenced
        (a new version row landed between scan and delete, and its put
        did not refresh the old mtime)."""

        def refs(self):  # noqa: ANN202
            return set()

        def contains(self, candidate):  # noqa: ANN001, ANN202
            return candidate == ref

    report = collect_orphan_blobs(store, _FlipProvider(), grace_seconds=GRACE, now=NOW)

    assert report.deleted == []
    assert report.kept_referenced == 1
    assert (store.root / ref).exists()


def test_gc_dry_run_reports_without_deleting(tmp_path):
    store = DocArtifactStore(root=tmp_path / "base")
    ref = store.put(b"dry-orphan", suffix=".bin")
    _age(store, ref, NOW - 48 * 3600)

    report = collect_orphan_blobs(store, set(), grace_seconds=GRACE, now=NOW, dry_run=True)

    assert report.deleted == []
    assert report.would_delete == [ref]
    assert (store.root / ref).exists()


def test_gc_missing_root_is_an_error_not_a_crash(tmp_path):
    store = DocArtifactStore(root=tmp_path / "never-created")
    report = collect_orphan_blobs(store, set(), grace_seconds=GRACE, now=NOW)
    assert report.deleted == []
    assert report.errors  # root missing is recorded, nothing deleted


# --------------------------------------------------------------------------- #
# Item 2 — CLI entry point                                                    #
# --------------------------------------------------------------------------- #
def _load_cli_module():
    spec = importlib.util.spec_from_file_location(
        "gc_orphan_blobs", _REPO_ROOT / "scripts" / "gc_orphan_blobs.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_refuses_nondurable_refs_then_sweeps_with_override(tmp_path, capsys):
    base = tmp_path / "base"
    store = DocArtifactStore(root=base)
    review = InMemoryArtifactReviewStore(blob_store=store)
    version, _ = review.create_pending_version(
        "t-1", artifact_id="art-1", content=b"referenced-content"
    )
    referenced_ref = version.content_ref
    assert referenced_ref is not None
    orphan_ref = store.put(b"cli-orphan", suffix=".bin")
    old = time.time() - 48 * 3600
    _age(store, referenced_ref, old)
    _age(store, orphan_ref, old)

    cli = _load_cli_module()
    set_artifact_review_store(review)
    try:
        # Without the override the CLI must refuse: the in-memory
        # referenced set is not durable, so nothing may be deleted.
        refused = cli.main(["--root", str(base)])
        assert refused == 2
        assert (store.root / orphan_ref).exists()

        swept = cli.main(["--root", str(base), "--allow-nondurable-refs"])
        assert swept == 0
        assert not (store.root / orphan_ref).exists()
        assert (store.root / referenced_ref).exists()
        out = capsys.readouterr().out
        assert "deleted=1" in out
    finally:
        reset_artifact_review_store()
