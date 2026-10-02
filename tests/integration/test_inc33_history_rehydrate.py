"""INC33 — restart history survives: ``workspace_runs`` headers rehydrate the store.

``GET /runs`` / ``GET /runs/{id}`` read the **process-local**
``orchestrator.MemoryRunStore`` (empty after a restart). The run headers ARE
persisted (``workspace_runs``, migration ``016``) but nothing read them back, so
the history column + detail went blank on restart while ``GET /workspace/sessions``
still answered — ADR-02 「跨重启可查」 was documented, not implemented.

INC40 — the backfill moved from a one-shot **global** startup step to a
**read-side, per-tenant** one (:func:`forgeflow.runtime.dispatcher.ensure_tenant_history`).
The old global window (``ORDER BY created_at DESC LIMIT 200`` with no tenant
filter) was non-deterministic: once the shared dev DB held >200 newer rows from
*other* tenants, this tenant's history was pushed out and the assertion went red
purely as the DB grew. This module now proves the effect through the **real read
path** (the request contract is ADR-02's actual promise) and adds a
multi-tenant stress nail that fails against the old global implementation.

Both backends run (each declares its own profile, per the repo's cross-backend
convention); ``postgres`` is the discriminating persistence round-trip.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import uuid
from datetime import datetime, timezone

import pytest

from forgeflow.config import get_settings
from forgeflow.runtime.dispatcher import get_run_dispatcher, reset_run_dispatcher
from forgeflow.runtime.orchestrator import RunRecord, get_run_store, reset_run_store
from forgeflow.workspace.models import WorkspaceRunRecord
from forgeflow.workspace.store import (
    MemoryWorkspaceStore,
    PgWorkspaceStore,
    get_workspace_store,
    reset_workspace_store,
)

pytestmark = pytest.mark.asyncio

_ROOT = pathlib.Path(__file__).resolve().parents[2]
TENANT = "t-inc33-rehydrate"


def _suffix() -> str:
    return uuid.uuid4().hex[:12]


def _now_iso() -> str:
    """Timezone-aware ISO-8601 UTC now — the record's own clock (hermetic)."""
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# profile activation (mirrors tests/integration/test_inc12_run_id_text_          #
# conformance.py: each backend declares its own profile)                        #
# --------------------------------------------------------------------------- #
def _postgres_reachable() -> bool:
    import psycopg

    dsn = get_settings().postgres_sync_url.replace(
        "postgresql+psycopg://", "postgresql://"
    )
    try:
        with psycopg.connect(dsn, connect_timeout=4) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True
    except Exception:  # noqa: BLE001 — any failure means "not usable here"
        return False


def _upgrade_head() -> None:
    child_env = dict(os.environ)
    child_env["POSTGRES_SYNC_URL"] = get_settings().postgres_sync_url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(_ROOT),
        env=child_env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade head failed:\n{result.stderr[-2000:]}")


def _require_postgres() -> None:
    if not _postgres_reachable():
        pytest.skip("live Postgres not reachable — rehydrate case needs it")
    try:
        _upgrade_head()
    except Exception as exc:  # noqa: BLE001 — an unusable DB means "skip here"
        pytest.skip(f"dev DB could not be migrated to head: {exc}")


@pytest.fixture(autouse=True)
def _clean_state():
    # The B2 rule: ``reset_run_dispatcher()`` also clears the hydration
    # idempotency set, so every case re-hydrates from a clean slate.
    reset_run_store()
    reset_run_dispatcher()
    reset_workspace_store()
    yield
    reset_run_store()
    reset_run_dispatcher()
    reset_workspace_store()


@pytest.fixture
def active_backend(request, monkeypatch):
    backend = request.param
    if backend == "memory":
        request.getfixturevalue("force_memory_backend")
        reset_workspace_store()
        yield "memory"
        return

    _require_postgres()
    monkeypatch.setattr(get_settings(), "storage_backend", "postgres")
    reset_workspace_store()
    try:
        yield "postgres"
    finally:
        reset_workspace_store()


def _record(run_id: str, tenant: str) -> RunRecord:
    """A terminal run carrying BOTH body fields (steps) and a persisted artifact.

    INC40 — ``created_at`` / ``completed_at`` use the **current** time, not a
    hard-coded 2026-09-30, so the assertions never depend on how many rows the
    shared dev DB happens to hold (the旧 non-hermetic defect).
    """
    now = _now_iso()
    return RunRecord(
        run_id=run_id,
        thread_id=f"{run_id}-th",
        tenant_id=tenant,
        agent_id=None,
        intent="重启前派发的任务",
        status="completed",
        outcome="success",
        steps=[{"index": 0, "tool": "report.render", "status": "ok"}],
        errors=[],
        created_at=now,
        completed_at=now,
        session_id=run_id,
        parent_run_id="",
        actor_user_id="u-inc33",
        actor_role="admin",
        artifacts=[{"id": "art-1", "title": "报告", "content": "正文"}],
    )


def _purge_own_tenant_rows(*tenants: str) -> None:
    """Delete only the rows this case created (memory profile ⇒ no-op).

    INC40 / B3 — deliberately **not** ``tests/conftest.py::pg_purge``: that helper
    runs ``DELETE FROM {table}`` with **no WHERE clause** (whole-table wipe), which
    would erase every other tenant/test's rows on the shared dev DB. This targeted
    delete reuses the same "own short-lived psycopg connection" technique but
    removes only the case's own tenant rows.
    """
    settings = get_settings()
    if settings.storage_backend.lower() != "postgres":
        return
    import psycopg

    dsn = settings.postgres_sync_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        for tenant in tenants:
            cur.execute("DELETE FROM workspace_runs WHERE tenant_id = %s", (tenant,))
        conn.commit()


@pytest.mark.parametrize("active_backend", ["memory", "postgres"], indirect=True)
async def test_history_survives_restart_and_detail_marked(active_backend):
    store = get_workspace_store()
    expected = PgWorkspaceStore if active_backend == "postgres" else MemoryWorkspaceStore
    assert isinstance(store, expected), (
        f"active_backend={active_backend!r} must drive get_workspace_store() to "
        f"{expected.__name__}, got {type(store).__name__}"
    )

    suffix = _suffix()
    tenant = f"{TENANT}-{suffix}"
    run_id = f"run-inc33-{suffix}"

    # Persist the run header exactly the way the dispatcher does (the durable copy).
    record = _record(run_id, tenant)
    await store.save(WorkspaceRunRecord.from_run_record(record))

    # --- simulate a process restart: the in-process run store is gone ---
    reset_run_store()
    assert get_run_store().get(run_id) is None, "the restart really emptied the store"

    # --- the REAL startup entry (mark stale) — must not error ---
    await get_run_dispatcher().reconcile_on_start()

    # (a') the history comes back through the REAL read path (not just a store peek).
    # INC40 — the assertion target is the request contract (ADR-02's real promise).
    from forgeflow.api.routers.runs import get_run as get_run_route
    from forgeflow.api.routers.runs import list_runs as list_runs_route

    listing = await list_runs_route(limit=50, tenant=tenant)
    assert run_id in {item.run_id for item in listing.items}, (
        "history must survive the restart (ADR-02)"
    )

    # (b) the detail path is honest: the execution detail did NOT survive …
    detail = await get_run_route(run_id, tenant=tenant)
    assert detail.run_id == run_id
    assert detail.status == "completed"
    assert detail.detail_retained is False  # ← the explicit honest marker
    assert detail.steps == []  # … genuinely gone, not a fabricated trail
    # … while the durable copy the header DOES carry (the artifacts) is intact.
    assert [a.get("id") for a in detail.artifacts] == ["art-1"]


@pytest.mark.parametrize("active_backend", ["memory", "postgres"], indirect=True)
async def test_tenant_history_survives_when_many_newer_other_tenant_rows(active_backend):
    """INC40 — with >200 newer rows from ANOTHER tenant, this tenant's history is
    still reachable.

    This is the multi-tenant stress nail the old global window lacked: the old
    ``list_recent(200)`` would return the 200 newest rows **across all tenants**,
    pushing this tenant's (older) row out of the window — the assertion would then
    fail. The tenant-scoped read removes that coupling entirely.
    """
    from forgeflow.api.routers.runs import get_run as get_run_route
    from forgeflow.api.routers.runs import list_runs as list_runs_route

    store = get_workspace_store()
    suffix = _suffix()
    tenant = f"{TENANT}-stress-{suffix}"
    other_tenant = f"t-other-{suffix}"
    run_id = f"run-inc33-stress-{suffix}"

    try:
        # 1) This tenant's ONE, deliberately OLD record.
        old = _record(run_id, tenant)
        old.created_at = "2000-01-01T00:00:00+00:00"
        old.completed_at = "2000-01-01T00:01:00+00:00"
        await store.save(WorkspaceRunRecord.from_run_record(old))

        # 2) Another tenant's 240 NEWER records (>200 — enough to fill the old
        #    global window and squeeze the target out).
        for i in range(240):
            other = _record(f"run-other-{suffix}-{i}", other_tenant)
            other.created_at = f"2999-01-01T00:{i % 60:02d}:00+00:00"
            other.completed_at = other.created_at
            await store.save(WorkspaceRunRecord.from_run_record(other))

        # 3) Restart: empty the in-process store + the hydration set.
        reset_run_store()
        await get_run_dispatcher().reconcile_on_start()

        # 4) This tenant's history is still reachable through the real read path.
        listing = await list_runs_route(limit=50, tenant=tenant)
        assert run_id in {item.run_id for item in listing.items}, (
            "本租户历史被其它租户的记录挤出窗口（ADR-02 在多租户下失效）"
        )
        detail = await get_run_route(run_id, tenant=tenant)
        assert detail.run_id == run_id
    finally:
        # INC40 / B3 — clean up ONLY this case's own rows (never the whole table).
        _purge_own_tenant_rows(tenant, other_tenant)
        reset_run_store()
        reset_run_dispatcher()
