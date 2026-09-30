"""INC33 — restart history survives: ``workspace_runs`` headers rehydrate the store.

``GET /runs`` / ``GET /runs/{id}`` read the **process-local**
``orchestrator.MemoryRunStore`` (empty after a restart). The run headers ARE
persisted (``workspace_runs``, migration ``016``) but nothing read them back, so
the history column + detail went blank on restart while ``GET /workspace/sessions``
still answered — ADR-02 「跨重启可查」 was documented, not implemented.
``RunDispatcher.reconcile_on_start`` now **hydrates** the persisted headers into
the in-process store, so both surfaces recover with **no** request-contract change.

This nail proves the **effect** through the real startup entry:
  persist a header → drop the in-process store (the restart) →
  ``reconcile_on_start()`` → the history source returns it AND the detail path is
  honest (``detail_retained is False`` — the execution detail did NOT survive, so
  the UI must say so rather than render a confident empty step list).

Both backends run (each declares its own profile, per the repo's cross-backend
convention); ``postgres`` is the discriminating persistence round-trip.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import uuid

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
    """A terminal run carrying BOTH body fields (steps) and a persisted artifact."""
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
        created_at="2026-09-30T00:00:00+00:00",
        completed_at="2026-09-30T00:01:00+00:00",
        session_id=run_id,
        parent_run_id="",
        actor_user_id="u-inc33",
        actor_role="admin",
        artifacts=[{"id": "art-1", "title": "报告", "content": "正文"}],
    )


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

    # --- the REAL startup entry (mark stale + hydrate) ---
    await get_run_dispatcher().reconcile_on_start()

    # (a) the history source returns the pre-restart run again.
    rehydrated = get_run_store().get(run_id)
    assert rehydrated is not None, "history must survive the restart (ADR-02)"
    assert rehydrated.status == "completed"
    assert rehydrated.intent == "重启前派发的任务"
    assert run_id in {r.run_id for r in get_run_store().list(tenant, limit=50)}

    # (a') and it comes back through the real route, not just the store.
    from forgeflow.api.routers.runs import get_run as get_run_route
    from forgeflow.api.routers.runs import list_runs as list_runs_route

    listing = await list_runs_route(limit=50, tenant=tenant)
    assert run_id in {item.run_id for item in listing.items}

    # (b) the detail path is honest: the execution detail did NOT survive …
    assert rehydrated.detail_retained is False
    detail = await get_run_route(run_id, tenant=tenant)
    assert detail.run_id == run_id
    assert detail.status == "completed"
    assert detail.detail_retained is False  # ← the explicit honest marker
    assert detail.steps == []  # … genuinely gone, not a fabricated trail
    # … while the durable copy the header DOES carry (the artifacts) is intact.
    assert [a.get("id") for a in detail.artifacts] == ["art-1"]
