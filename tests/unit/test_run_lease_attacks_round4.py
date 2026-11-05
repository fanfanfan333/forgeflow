"""PR #6 review round 4 — regression tests for the two follow-ups.

Each test pins one round-4 finding against the round-3 head
(``4edb074``) and fails there:

1. Cached process-local stores survived ``fork()``: the registry
   handed back the inherited ``_STORE`` and the store objects
   themselves served the child from a divergent private copy, so
   parent and child could each mint fencing token 1 for the same
   run (and each accept the same processed event id).  The stores
   now record their creating pid, refuse every operation from any
   other process, and the registries invalidate their caches in the
   child via ``os.register_at_fork``.
2. The Postgres pending-version commit wrote its rows BEFORE the
   blob (round 3's reorder), so a crash in between left a committed
   version pointing at content that does not exist.  The blob —
   content-addressed, invisible while unreferenced — is now written
   FIRST and the fenced transaction commits the reference second;
   a refusal can strand an orphan blob, never a record without
   content.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from forgeflow.documents import DocArtifactStore
from forgeflow.documents.review_store import (
    ArtifactNotFound,
    PostgresArtifactReviewStore,
)
from forgeflow.events.processed_events import (
    EventDedupeConfigurationError,
    InMemoryProcessedEventStore,
    get_processed_event_store,
    reset_processed_event_store,
)
from forgeflow.runtime.run_lease import (
    InMemoryRunLeaseStore,
    LeaseFencedError,
    RunLease,
    RunLeaseConfigurationError,
    RunLeaseSession,
    current_run_lease,
    get_run_lease_store,
    reset_run_lease_store,
)

_TENANT = "t-1"

requires_fork = pytest.mark.skipif(
    not hasattr(os, "fork"), reason="os.fork is unavailable on this platform"
)


@pytest.fixture(autouse=True)
def _clean_registries():
    yield
    reset_run_lease_store()
    reset_processed_event_store()


# --------------------------------------------------------------------------- #
# Fork helpers — pipes only for synchronisation, no sleeps.  The child
# reports through a pipe with os.write and leaves via os._exit: it must
# never run pytest teardown or flush inherited stdio buffers.
# --------------------------------------------------------------------------- #
def _fork_probe(probe) -> str:  # noqa: ANN001, ANN202
    """Run ``probe()`` in a forked child; return the text it reports."""
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            try:
                text = str(probe())
            except BaseException as exc:  # noqa: BLE001 — report, never raise
                text = f"PROBE-RAISED {type(exc).__name__}: {exc}"
            os.write(write_fd, text.encode())
        finally:
            os._exit(0)
    os.close(write_fd)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(read_fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    os.close(read_fd)
    _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    return b"".join(chunks).decode()


def _fork_synced(parent_body, child_body) -> str:  # noqa: ANN001, ANN202
    """Fork; the child waits for the parent's signal, then runs
    ``child_body()`` and reports its returned text.

    ``parent_body()`` runs in the parent AFTER the fork and BEFORE
    the signal — this is what pins the divergent-copy interleaving
    (parent mutates its store copy while the child still holds the
    pre-fork snapshot) deterministically.
    """
    go_r, go_w = os.pipe()
    out_r, out_w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(go_w)
        os.close(out_r)
        try:
            os.read(go_r, 1)
            try:
                text = str(child_body())
            except BaseException as exc:  # noqa: BLE001 — report, never raise
                text = f"PROBE-RAISED {type(exc).__name__}: {exc}"
            os.write(out_w, text.encode())
        finally:
            os._exit(0)
    os.close(go_r)
    os.close(out_w)
    try:
        parent_body()
        os.write(go_w, b"x")
    finally:
        os.close(go_w)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(out_r, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    os.close(out_r)
    _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    return b"".join(chunks).decode()


# --------------------------------------------------------------------------- #
# Item 1a — a forked child is refused by the INHERITED lease store,
# through a direct reference (no registry re-resolution involved).
# --------------------------------------------------------------------------- #
@requires_fork
def test_forked_child_refused_by_inherited_lease_store():
    reset_run_lease_store()
    store = get_run_lease_store()
    assert isinstance(store, InMemoryRunLeaseStore)
    lease = store.acquire(_TENANT, "run-fork-a", "worker-a", 60.0)
    assert lease is not None and lease.fencing_token == 1

    def probe() -> str:
        attempts = {
            "peek": lambda: store.peek(_TENANT, "run-fork-a"),
            "validate": lambda: store.validate(_TENANT, "run-fork-a", "worker-a", 1),
            "renew": lambda: store.renew(lease, 60.0),
            "release": lambda: store.release(lease),
            "acquire": lambda: store.acquire(_TENANT, "run-fork-a", "worker-child", 60.0),
        }
        lines = []
        for label, attempt in attempts.items():
            try:
                lines.append(f"{label}:served:{attempt()!r}")
            except Exception as exc:  # noqa: BLE001 — classify and report
                lines.append(f"{label}:raised:{type(exc).__name__}")
        return "\n".join(lines)

    report = _fork_probe(probe)
    # On the pre-fix head every attempt is SERVED from the child's
    # inherited copy (release even succeeds there, after which the
    # child mints token 2 for a run the parent still holds at
    # token 1 — two divergent "current" states for one run).
    for label in ("peek", "validate", "renew", "release", "acquire"):
        assert f"{label}:raised:{RunLeaseConfigurationError.__name__}" in report, report
    # The child's attempts must not have disturbed the parent at all.
    assert store.validate(_TENANT, "run-fork-a", "worker-a", 1) is True
    holder = store.peek(_TENANT, "run-fork-a")
    assert holder is not None and holder.owner == "worker-a"


@requires_fork
def test_forked_child_cannot_mint_token_one_for_parents_run():
    """The literal divergence attack: fork FIRST, then the parent
    acquires the run (token 1 in the parent's copy).  The child's
    inherited copy never saw that acquisition — pre-fix it mints
    token 1 for the same run, and two processes execute one run."""
    reset_run_lease_store()
    store = get_run_lease_store()
    assert isinstance(store, InMemoryRunLeaseStore)
    acquired: dict[str, Any] = {}

    def parent_body() -> None:
        lease = store.acquire(_TENANT, "run-fork-diverge", "worker-a", 60.0)
        assert lease is not None
        acquired["lease"] = lease

    def child_body() -> str:
        try:
            lease = store.acquire(_TENANT, "run-fork-diverge", "worker-child", 60.0)
        except Exception as exc:  # noqa: BLE001 — classify and report
            return f"raised:{type(exc).__name__}"
        return f"served:token={lease.fencing_token if lease else None}"

    report = _fork_synced(parent_body, child_body)
    assert acquired["lease"].fencing_token == 1
    assert report == f"raised:{RunLeaseConfigurationError.__name__}", report
    # Still exactly one holder, in the parent.
    assert store.validate(_TENANT, "run-fork-diverge", "worker-a", 1) is True


@requires_fork
def test_forked_child_refused_by_inherited_processed_event_store():
    """Same divergence for replay protection: fork first, then the
    parent claims an event id.  Pre-fix the child's inherited copy
    has never seen the claim and ACCEPTS the same id — the replay
    the registry exists to stop."""
    reset_processed_event_store()
    store = get_processed_event_store()
    assert isinstance(store, InMemoryProcessedEventStore)
    retention = datetime.now(UTC) + timedelta(hours=1)
    claimed: dict[str, Any] = {}

    def parent_body() -> None:
        claimed["ok"] = store.claim("evt-fork-r4", retention_until=retention, tenant_id=_TENANT)

    def child_body() -> str:
        try:
            accepted = store.claim("evt-fork-r4", retention_until=retention, tenant_id=_TENANT)
        except Exception as exc:  # noqa: BLE001 — classify and report
            return f"raised:{type(exc).__name__}"
        return f"served:accepted={accepted}"

    report = _fork_synced(parent_body, child_body)
    assert claimed["ok"] is True
    assert report == f"raised:{EventDedupeConfigurationError.__name__}", report
    # The parent's claim still dedupes normally in the parent.
    assert store.claim("evt-fork-r4", retention_until=retention, tenant_id=_TENANT) is False


@requires_fork
def test_forked_child_reresolution_fails_closed_instead_of_inherited_cache():
    """Item 1c: ``get_run_lease_store()`` in the child must NOT hand
    back the inherited cached store.  The at-fork hook invalidates
    the cache, re-resolution runs the full confirmation path, and
    the parent's held lock fails it closed."""
    reset_run_lease_store()
    store = get_run_lease_store()
    assert isinstance(store, InMemoryRunLeaseStore)

    def probe() -> str:
        try:
            resolved = get_run_lease_store()
        except Exception as exc:  # noqa: BLE001 — classify and report
            return f"raised:{type(exc).__name__}"
        return f"served:same-object={resolved is store}"

    report = _fork_probe(probe)
    # Pre-fix this is "served:same-object=True" — the inherited
    # cache short-circuits every confirmation check.
    assert report == f"raised:{RunLeaseConfigurationError.__name__}", report


# --------------------------------------------------------------------------- #
# Item 2 — Postgres blob ordering, driven through a recording fake.
# No live PG exists in this environment; the fake connection honours
# the psycopg contract the store relies on (explicit commit() makes
# staged rows visible; an exception leaving the block rolls back).
# --------------------------------------------------------------------------- #
class _RecordingBlobStore:
    """Blob-store double: content-addressed refs (the real rule),
    bytes in a dict, every put recorded in the shared event log."""

    def __init__(self, events: list) -> None:
        self._events = events
        self._blobs: dict[str, bytes] = {}

    def ref_for(self, data: bytes | bytearray, suffix: str = ".docx") -> str:
        return DocArtifactStore.ref_for(bytes(data), suffix=suffix)

    def put(self, data: bytes | bytearray, suffix: str = ".docx") -> str:
        ref = self.ref_for(data, suffix=suffix)
        self._events.append(("blob-put", ref))
        self._blobs[ref] = bytes(data)
        return ref

    def exists(self, ref: str) -> bool:
        return ref in self._blobs

    def get(self, ref: str) -> bytes:
        return self._blobs[ref]

    def delete(self, ref: str) -> bool:
        return self._blobs.pop(ref, None) is not None


class _FakePgDatabase:
    """Committed state shared by every fake connection of a test."""

    def __init__(self, events: list, blob_store: _RecordingBlobStore) -> None:
        self.events = events
        self.blob_store = blob_store
        self.versions: dict[tuple, tuple] = {}
        self.reviews: dict[tuple, tuple] = {}
        self.lease_row_present = True
        #: content_refs that were committed while their blob was
        #: missing — the exact record-without-content window.
        self.commit_violations: list[str] = []


class _FakePgCursor:
    def __init__(self, conn: _FakePgConnection) -> None:
        self._conn = conn
        self._result: list[tuple] = []
        self.rowcount = 0

    def __enter__(self) -> _FakePgCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        db = self._conn._db
        normalized = " ".join(str(sql).split()).upper()
        params = tuple(params or ())
        self._result = []
        if "FROM RUN_LEASES" in normalized:
            db.events.append(("fence-select",))
            self._result = [(1,)] if db.lease_row_present else []
        elif normalized.startswith("INSERT INTO ARTIFACT_VERSIONS"):
            db.events.append(("insert-version", params[2]))
            row = tuple(params)
            self._conn._staged_versions[(params[0], params[1], int(params[2]))] = row
        elif normalized.startswith("INSERT INTO ARTIFACT_REVIEWS"):
            db.events.append(("insert-review", params[1]))
            self._conn._staged_reviews[(params[0], params[1])] = tuple(params)
        elif "TENANT_ID = %S AND ARTIFACT_ID = %S AND VERSION = %S" in normalized:
            row = db.versions.get((params[0], params[1], int(params[2])))
            self._result = [row] if row is not None else []
        elif "ARTIFACT_ID = %S AND VERSION = %S" in normalized:
            match = [
                r for (_t, a, v), r in db.versions.items() if a == params[0] and v == int(params[1])
            ]
            self._result = match[:1]
        elif "FROM ARTIFACT_VERSIONS" in normalized:
            rows = [r for (t, _a, _v), r in db.versions.items() if t == params[0]]
            if len(params) > 1:
                rows = [r for r in rows if r[1] == params[1]]
            rows.sort(key=lambda r: (r[1], int(r[2])))
            self._result = rows
        elif normalized.startswith("DELETE FROM ARTIFACT_VERSIONS"):
            db.events.append(("delete-version", params[2]))
            db.versions.pop((params[0], params[1], int(params[2])), None)
        elif normalized.startswith("DELETE FROM ARTIFACT_REVIEWS"):
            db.events.append(("delete-review", params[1]))
            db.reviews.pop((params[0], params[1]), None)
        else:  # pragma: no cover — a statement the fake does not model
            raise AssertionError(f"unmodelled SQL in fake PG: {sql!r}")

    def fetchone(self) -> tuple | None:
        return self._result[0] if self._result else None

    def fetchall(self) -> list[tuple]:
        return list(self._result)


class _FakePgConnection:
    """One fake connection: writes stage locally until commit()."""

    def __init__(self, db: _FakePgDatabase) -> None:
        self._db = db
        self._staged_versions: dict[tuple, tuple] = {}
        self._staged_reviews: dict[tuple, tuple] = {}

    def __enter__(self) -> _FakePgConnection:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        # psycopg contract the store relies on: leaving the block
        # with an exception rolls the transaction back (staged rows
        # vanish); committed rows were already moved by commit().
        self._staged_versions.clear()
        self._staged_reviews.clear()
        return False

    def cursor(self) -> _FakePgCursor:
        return _FakePgCursor(self)

    def commit(self) -> None:
        db = self._db
        # The ordering invariant, checked at the exact moment staged
        # rows become visible: a version row may only ever commit
        # when the blob it references already exists.
        for row in self._staged_versions.values():
            content_ref = row[5]  # _VERSION_COLUMNS order
            if not db.blob_store.exists(content_ref):
                db.commit_violations.append(content_ref)
        db.versions.update(self._staged_versions)
        db.reviews.update(self._staged_reviews)
        self._staged_versions.clear()
        self._staged_reviews.clear()
        db.events.append(("commit",))

    def rollback(self) -> None:
        self._staged_versions.clear()
        self._staged_reviews.clear()


class _FakePgReviewStore(PostgresArtifactReviewStore):
    def __init__(self, db: _FakePgDatabase, blob_store: _RecordingBlobStore) -> None:
        super().__init__("postgresql://fake.invalid/forgeflow", blob_store=blob_store)
        self._db = db

    def _connect(self, tenant_id: str | None = None) -> _FakePgConnection:  # noqa: ANN202
        return _FakePgConnection(self._db)


class _StubPostgresLeaseStore:
    """Lease-store identity for the session: the in-transaction
    fence consults the fake DB's ``run_leases`` state, so the stub
    only needs the backend marker + a validate that passes (the
    pre-transaction ``assert_fenced_commit`` path)."""

    backend = "postgres"

    def validate(self, tenant_id: str, run_id: str, owner: str, fencing_token: int) -> bool:
        return True


def _pg_harness(*, lease_row_present: bool):
    events: list = []
    blob_store = _RecordingBlobStore(events)
    db = _FakePgDatabase(events, blob_store)
    db.lease_row_present = lease_row_present
    store = _FakePgReviewStore(db, blob_store)
    lease = RunLease(
        tenant_id=_TENANT,
        run_id="run-pg-r4",
        owner="worker-a",
        fencing_token=7,
        acquired_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    session = RunLeaseSession(store=_StubPostgresLeaseStore(), lease=lease, ttl_seconds=300.0)
    token = current_run_lease.set(session)
    return events, db, blob_store, store, token


def test_pg_blob_is_written_before_the_fenced_commit():
    events, db, _blob_store, store, token = _pg_harness(lease_row_present=True)
    try:
        version, _review = store.create_pending_version(
            _TENANT,
            artifact_id="art-pg-r4",
            content=b"round-4 content",
            fmt="text",
            run_id="run-pg-r4",
        )
    finally:
        current_run_lease.reset(token)
    kinds = [event[0] for event in events]
    # Content first, reference second — the exact reverse of the
    # round-3 head, where "commit" precedes "blob-put" and the
    # commit-time invariant check below fires.
    assert kinds.index("blob-put") < kinds.index("commit"), events
    assert kinds.index("fence-select") < kinds.index("commit"), events
    assert db.commit_violations == []
    # And the committed version actually resolves to its content:
    # at no point did a readable record point at missing bytes.
    assert version.version == 1
    assert store.list_versions(_TENANT, "art-pg-r4") != []
    assert store.read_content(_TENANT, "art-pg-r4", 1) == b"round-4 content"


def test_pg_fence_refusal_commits_nothing_and_strands_only_an_orphan_blob():
    events, db, blob_store, store, token = _pg_harness(lease_row_present=False)
    try:
        with pytest.raises(LeaseFencedError):
            store.create_pending_version(
                _TENANT,
                artifact_id="art-pg-r4",
                content=b"round-4 refused",
                fmt="text",
                run_id="run-pg-r4",
            )
    finally:
        current_run_lease.reset(token)
    # Nothing was committed — the fence rolled the transaction back.
    assert db.versions == {}
    assert db.reviews == {}
    assert ("commit",) not in events
    # The content-first design's accepted residue: the blob WAS
    # written (pre-fix it never is — the put came after the commit)
    # and is now an unreferenced orphan: present in the blob store,
    # invisible through every read path, because no version row
    # points at it.
    ref = DocArtifactStore.ref_for(b"round-4 refused", suffix=".txt")
    assert blob_store.exists(ref)
    assert store.list_versions(_TENANT, "art-pg-r4") == []
    with pytest.raises(ArtifactNotFound):
        store.read_content(_TENANT, "art-pg-r4", 1)
