"""Blob durability (directory fsync) + orphan-blob REPORT (read-only).

Item 1: ``DocArtifactStore.put`` fsyncs the file but (before the fix)
never the directory — a rename is not durable until its containing
directory is fsynced, and a newly created directory entry is not
durable until its parent is.  The tests record os.open/os.fsync/
os.replace and assert the directory fds are fsynced, in order.
``_fsync_dir`` skips ONLY where the platform cannot open directories
(Windows, ``os.name == "nt"``); on POSIX an open failure propagates.

Item 2 (scope-reduced after retest): the orphan-blob tool was a
deleting GC; because the referenced set is INCOMPLETE (refs held only
in run records / tool payloads are not enumerable), it could delete
blobs still in use.  It is now a read-only report:
``forgeflow.documents.blob_gc.report_orphan_blobs`` lists candidates
(unreferenced in the enumerated set AND older than the grace period)
with sizes, and never deletes anything.  Tenant-enumeration failure
in the Postgres review store raises instead of returning a partial
set.  ``put``'s early return refreshes the blob's mtime.

The report runner below resolves the report function by name so that
on the pre-fix head (where the tool still deletes) these tests fail
*behaviourally* — files actually vanish — rather than only at import.
"""

from __future__ import annotations

import importlib.util
import os
import time
from pathlib import Path
from typing import Any

import pytest

from forgeflow.documents import blob_gc
from forgeflow.documents.blob_gc import ReviewStoreRefProvider
from forgeflow.documents.review_store import (
    InMemoryArtifactReviewStore,
    PostgresArtifactReviewStore,
    reset_artifact_review_store,
    set_artifact_review_store,
)
from forgeflow.documents.store import DocArtifactStore, _fsync_dir

_REPO_ROOT = Path(__file__).resolve().parents[2]

NOW = 1_800_000_000.0  # fixed epoch the report ages are measured against
GRACE = 24.0 * 3600


def _run_report(store, refs, *, grace_seconds=GRACE, now=NOW):  # noqa: ANN001, ANN202
    """Run the orphan-blob report; on the pre-fix head this resolves
    to the old deleting collector, so the no-deletion assertions fail
    on real behaviour (the orphan is actually deleted)."""
    fn = getattr(blob_gc, "report_orphan_blobs", None)
    if fn is None:  # pragma: no cover — pre-fix head only
        fn = blob_gc.collect_orphan_blobs
    return fn(store, refs, grace_seconds=grace_seconds, now=now)


def _candidate_refs(report) -> list[str]:  # noqa: ANN001
    return [c.ref for c in report.candidates]


def _age(store: DocArtifactStore, ref: str, mtime: float) -> None:
    path = store.root / ref
    os.utime(path, (mtime, mtime))


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


def test_fsync_dir_skips_only_on_windows(tmp_path, monkeypatch):
    """Control: the ONLY skip is the platform that cannot open
    directories (Windows).  With ``os.name`` patched to ``nt`` the
    helper must return without even attempting an open."""
    monkeypatch.setattr(os, "name", "nt")

    def forbidden_open(path, flags, *args, **kwargs):  # noqa: ANN001, ANN202
        raise AssertionError("os.open must not be attempted on Windows")

    monkeypatch.setattr(os, "open", forbidden_open)
    assert _fsync_dir(tmp_path) is None


def test_put_propagates_directory_open_errors_on_posix(tmp_path, monkeypatch):
    """On POSIX an ``os.open`` failure on a directory (permissions,
    fd exhaustion, …) is a real error and must propagate out of put —
    it must NOT be swallowed as "platform cannot open directories"."""
    store = DocArtifactStore(root=tmp_path / "base")
    real_open = os.open

    def guarded_open(path, flags, *args, **kwargs):  # noqa: ANN001, ANN202
        if os.path.isdir(path):
            raise PermissionError("simulated POSIX open failure on a directory")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded_open)
    with pytest.raises(PermissionError):
        store.put(b"must-not-pretend-durable")


# --------------------------------------------------------------------------- #
# Item 4 — put() early return refreshes mtime                                 #
# --------------------------------------------------------------------------- #
def test_put_early_return_refreshes_blob_mtime(tmp_path):
    store = DocArtifactStore(root=tmp_path / "base")
    content = b"re-referenced-bytes"
    ref = store.put(content)
    path = store.root / ref
    old = time.time() - 7 * 24 * 3600
    os.utime(path, (old, old))
    assert path.stat().st_mtime == pytest.approx(old, abs=1.0)

    # Same bytes again: the exists-with-matching-size early return.
    assert store.put(content) == ref

    refreshed = path.stat().st_mtime
    assert refreshed > old + 24 * 3600  # no longer looks a week old
    assert abs(refreshed - time.time()) < 120  # refreshed to ~now
    assert store.get(ref) == content


# --------------------------------------------------------------------------- #
# Item 2 — orphan-blob report (read-only)                                     #
# --------------------------------------------------------------------------- #
def test_report_never_deletes_and_lists_old_orphan_as_candidate(tmp_path, monkeypatch):
    store = DocArtifactStore(root=tmp_path / "base")
    review = InMemoryArtifactReviewStore(blob_store=store)
    version, _ = review.create_pending_version(
        "t-1", artifact_id="art-1", content=b"referenced-content"
    )
    referenced_ref = version.content_ref
    assert referenced_ref is not None
    orphan_ref = store.put(b"orphan-bytes", suffix=".bin")
    fresh_ref = store.put(b"fresh-orphan", suffix=".bin")
    _age(store, orphan_ref, NOW - 48 * 3600)
    _age(store, referenced_ref, NOW - 48 * 3600)
    _age(store, fresh_ref, NOW - 3600)  # within grace

    delete_calls: list[str] = []
    real_delete = DocArtifactStore.delete

    def delete_spy(self, ref):  # noqa: ANN001, ANN202
        delete_calls.append(ref)
        return real_delete(self, ref)

    remove_calls: list[str] = []
    real_remove, real_unlink = os.remove, os.unlink

    def remove_spy(path, *args, **kwargs):  # noqa: ANN001, ANN202
        remove_calls.append(os.fspath(path))
        return real_remove(path, *args, **kwargs)

    def unlink_spy(path, *args, **kwargs):  # noqa: ANN001, ANN202
        remove_calls.append(os.fspath(path))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(DocArtifactStore, "delete", delete_spy)
    monkeypatch.setattr(os, "remove", remove_spy)
    monkeypatch.setattr(os, "unlink", unlink_spy)

    report = _run_report(store, ReviewStoreRefProvider(review))

    # NOTHING was deleted, through any path, and every file survives.
    assert delete_calls == []
    assert remove_calls == []
    for ref in (orphan_ref, referenced_ref, fresh_ref):
        assert (store.root / ref).exists(), f"report deleted a blob: {ref}"
    # The report lists exactly the old orphan, with its size.
    assert report.scanned == 3
    assert report.kept_referenced == 1
    assert report.kept_within_grace == 1
    assert report.errors == []
    assert _candidate_refs(report) == [orphan_ref]
    candidate = report.candidates[0]
    assert candidate.size_bytes == len(b"orphan-bytes")
    assert candidate.age_seconds == pytest.approx(48 * 3600)
    assert report.candidate_bytes == len(b"orphan-bytes")


def test_report_keeps_within_grace_orphan_unlisted(tmp_path):
    store = DocArtifactStore(root=tmp_path / "base")
    ref = store.put(b"fresh-orphan", suffix=".bin")
    _age(store, ref, NOW - 3600)  # one hour old, grace is 24h

    report = _run_report(store, set())

    assert _candidate_refs(report) == []
    assert report.kept_within_grace == 1
    assert (store.root / ref).exists()


def test_report_shared_ref_candidate_only_after_last_row_removed(tmp_path):
    store = DocArtifactStore(root=tmp_path / "base")
    review = InMemoryArtifactReviewStore(blob_store=store)
    v1, _ = review.create_pending_version("t-1", artifact_id="art-1", content=b"shared-content")
    v2, _ = review.create_pending_version("t-1", artifact_id="art-2", content=b"shared-content")
    ref = v1.content_ref
    assert ref is not None and v2.content_ref == ref  # identical bytes share the ref
    _age(store, ref, NOW - 72 * 3600)
    provider = ReviewStoreRefProvider(review)

    report = _run_report(store, provider)
    assert _candidate_refs(report) == []
    assert report.kept_referenced == 1

    # Remove ONE of the two referencing rows — still referenced.
    key1 = next(k for k in review._versions if k[1] == "art-1")
    del review._versions[key1]
    report = _run_report(store, provider)
    assert _candidate_refs(report) == []
    assert (store.root / ref).exists()

    # Remove the last referencing row — now it is REPORTED as a
    # candidate, but (report-only) the blob itself still exists.
    key2 = next(k for k in review._versions if k[1] == "art-2")
    del review._versions[key2]
    report = _run_report(store, provider)
    assert _candidate_refs(report) == [ref]
    assert (store.root / ref).exists()


def test_report_never_touches_tmp_or_non_scheme_files(tmp_path):
    store = DocArtifactStore(root=tmp_path / "base")
    control = store.put(b"control-blob", suffix=".bin")
    _age(store, control, NOW - 3600)  # within grace — the only real blob

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

    report = _run_report(store, set())

    assert report.scanned == 1  # only the scheme-conforming blob was scanned
    assert _candidate_refs(report) == []
    for path in (tmp_file, foreign, bad_shard_file, bad_name):
        assert path.exists(), f"report touched a non-candidate: {path}"


def test_report_missing_root_is_an_error_not_a_crash(tmp_path):
    store = DocArtifactStore(root=tmp_path / "never-created")
    report = _run_report(store, set())
    assert _candidate_refs(report) == []
    assert report.errors  # root missing is recorded, nothing listed


# --------------------------------------------------------------------------- #
# Item 2 — Postgres tenant enumeration failure raises                         #
# --------------------------------------------------------------------------- #
class _FakeEnumCursor:
    """Minimal psycopg-cursor fake modelling ONLY the enumeration
    queries (``_refs_visible_to`` / ``_ref_visible_to`` /
    ``_gc_tenant_ids``) against a per-tenant-pin ref map."""

    def __init__(self, conn: _FakeEnumConnection) -> None:
        self._conn = conn
        self._result: list[tuple] = []

    def __enter__(self) -> _FakeEnumCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        normalized = " ".join(str(sql).split()).upper()
        self._result = []
        if "FROM TENANT_MEMBERSHIPS" in normalized:
            if self._conn.store._fail_tenants:
                raise RuntimeError("tenant registry unavailable")
            self._result = [(t,) for t in self._conn.store._tenants]
        elif "FROM ARTIFACT_VERSIONS" in normalized:
            visible = self._conn.store._refs_by_pin.get(self._conn.tenant, set())
            if params:  # _ref_visible_to: SELECT 1 ... WHERE content_ref = %s
                self._result = [(1,)] if params[0] in visible else []
            else:  # _refs_visible_to: SELECT DISTINCT content_ref ...
                self._result = [(r,) for r in sorted(visible)]
        else:  # pragma: no cover — a statement the fake does not model
            raise AssertionError(f"unmodelled SQL in fake PG: {sql!r}")

    def fetchone(self) -> tuple | None:
        return self._result[0] if self._result else None

    def fetchall(self) -> list[tuple]:
        return list(self._result)


class _FakeEnumConnection:
    def __init__(self, store: _FakeEnumStore, tenant: str | None) -> None:
        self.store = store
        self.tenant = tenant

    def __enter__(self) -> _FakeEnumConnection:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def cursor(self) -> _FakeEnumCursor:
        return _FakeEnumCursor(self)


class _FakeEnumStore(PostgresArtifactReviewStore):
    """Postgres review store over the enumeration fake."""

    def __init__(
        self,
        refs_by_pin: dict[str | None, set[str]],
        *,
        tenants: list[str],
        fail_tenants: bool,
    ) -> None:
        super().__init__("postgresql://fake.invalid/forgeflow")
        self._refs_by_pin = refs_by_pin
        self._tenants = tenants
        self._fail_tenants = fail_tenants

    def _connect(self, tenant_id: str | None = None) -> _FakeEnumConnection:  # noqa: ANN202
        return _FakeEnumConnection(self, tenant_id)


def test_pg_enumeration_unions_session_and_tenant_refs_control():
    """Control (passes before and after): with a healthy registry the
    enumeration unions the session-visible refs with every tenant's."""
    store = _FakeEnumStore(
        {None: {"aa/session-ref"}, "t-1": {"bb/tenant-ref"}},
        tenants=["t-1"],
        fail_tenants=False,
    )
    assert store.referenced_content_refs() == {"aa/session-ref", "bb/tenant-ref"}


def test_pg_tenant_enumeration_failure_raises():
    store = _FakeEnumStore(
        {None: {"aa/session-ref"}, "t-1": {"bb/tenant-ref"}},
        tenants=["t-1"],
        fail_tenants=True,
    )
    # The snapshot must raise — pre-fix it silently fell back to the
    # session-visible set and reported it as complete.
    with pytest.raises(RuntimeError, match="tenant registry unavailable"):
        store.referenced_content_refs()
    # has_content_ref shares the same helper: an unseen ref must also
    # raise rather than answer False from a partial enumeration.
    with pytest.raises(RuntimeError, match="tenant registry unavailable"):
        store.has_content_ref("zz/unseen-ref")


# --------------------------------------------------------------------------- #
# Item 2 — CLI entry point (report-only)                                      #
# --------------------------------------------------------------------------- #
def _load_cli_module():
    spec = importlib.util.spec_from_file_location(
        "gc_orphan_blobs", _REPO_ROOT / "scripts" / "gc_orphan_blobs.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_reports_candidates_and_deletes_nothing(tmp_path, capsys):
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
        rc = cli.main(["--root", str(base)])
    finally:
        reset_artifact_review_store()

    assert rc == 0
    captured = capsys.readouterr()
    # The in-memory caveat is a printed WARNING now, not a refusal.
    assert "WARNING" in captured.err
    assert "UPPER BOUND" in captured.err
    # The candidate list names the orphan (with its size), not the
    # referenced blob; nothing claims a deletion happened.
    assert f"candidate {orphan_ref} size={len(b'cli-orphan')}" in captured.out
    assert f"candidate {referenced_ref}" not in captured.out
    assert "candidates=1" in captured.out
    assert "deleted" not in captured.out.replace("nothing is deleted", "")
    # And on disk: everything is still there.
    assert (store.root / orphan_ref).exists()
    assert (store.root / referenced_ref).exists()


def test_cli_fails_visibly_when_enumeration_raises(tmp_path, capsys):
    base = tmp_path / "base"
    store = DocArtifactStore(root=base)
    orphan_ref = store.put(b"cli-enum-orphan", suffix=".bin")
    _age(store, orphan_ref, time.time() - 48 * 3600)

    failing = _FakeEnumStore({None: set()}, tenants=["t-1"], fail_tenants=True)
    cli = _load_cli_module()
    set_artifact_review_store(failing)
    try:
        rc = cli.main(["--root", str(base)])
    finally:
        reset_artifact_review_store()

    captured = capsys.readouterr()
    assert rc != 0
    assert "tenant registry unavailable" in captured.err
    # No candidate list was printed from the partial enumeration…
    assert orphan_ref not in captured.out
    assert "candidate_bytes" not in captured.out
    # …and of course nothing was deleted.
    assert (store.root / orphan_ref).exists()
