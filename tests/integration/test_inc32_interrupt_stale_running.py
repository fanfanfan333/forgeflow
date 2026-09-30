"""INC32 — restart reconciliation really rewrites stale ``running`` rows.

``PgWorkspaceStore.interrupt_stale_running`` used to run::

    UPDATE workspace_runs SET status=$1, updated_at=$2
    WHERE status NOT IN (…) RETURNING count(*) AS n

PostgreSQL **forbids** aggregates in ``RETURNING``
(``GroupingError: aggregate functions are not allowed in RETURNING``), so the
statement raised on every call. Both call sites — ``RunDispatcher.reconcile_on_start``
and ``api.main``'s startup hook — swallow it in a broad ``except`` and only log
``"workspace startup reconciliation skipped: …"``. The consequence: **ADR-04's
"mark leftover ``running`` rows ``interrupted``" never took effect on the
postgres profile**, so a crash left fake ``running`` rows behind. The memory
backend is a plain dict walk and correct, so the whole suite stayed green while
the postgres profile degraded silently — which is why it slipped past two QA
passes (memory green, postgres warning-only).

This nail asserts the **effect**, not merely "no exception": it seeds one
non-terminal row and one terminal row, calls ``interrupt_stale_running()``, and
checks the non-terminal row was *actually* rewritten to ``interrupted`` while
the terminal row was left untouched. It runs on **both** backends (each
parameter declares its own profile, per the repo's cross-backend convention);
the ``postgres`` parameter is the discriminating one — the old
``RETURNING count(*)`` makes it error, so this nail is load-bearing there.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import uuid

import pytest

from forgeflow.config import get_settings
from forgeflow.workspace.models import WorkspaceRunRecord
from forgeflow.workspace.store import (
    INTERRUPTED_STATUS,
    MemoryWorkspaceStore,
    PgWorkspaceStore,
    get_workspace_store,
    reset_workspace_store,
)

pytestmark = pytest.mark.asyncio

_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _suffix() -> str:
    """Fresh marker suffix so re-runs seed new rows instead of clobbering."""
    return uuid.uuid4().hex[:12]


# --------------------------------------------------------------------------- #
# profile activation (mirrors tests/integration/test_inc12_run_id_text_          #
# conformance.py: each backend declares its own profile)                        #
# --------------------------------------------------------------------------- #
def _postgres_reachable() -> bool:
    """True when the configured dev Postgres answers a trivial query."""
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
    """Apply migrations to ``head`` in a subprocess (mirrors the PG suites)."""
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
    """Skip cleanly unless a live, migratable dev Postgres is available."""
    if not _postgres_reachable():
        pytest.skip("live Postgres not reachable — stale-running case needs it")
    try:
        _upgrade_head()
    except Exception as exc:  # noqa: BLE001 — an unusable DB means "skip here"
        pytest.skip(f"dev DB could not be migrated to head: {exc}")


@pytest.fixture(autouse=True)
def _clean_state():
    """Never inherit a store (or its cached backend) from a previous test."""
    reset_workspace_store()
    yield
    reset_workspace_store()


@pytest.fixture
def active_backend(request, monkeypatch):
    """Activate the parametrised storage backend for one case.

    ``memory`` **declares** its profile through ``force_memory_backend`` (never
    inherits it). ``postgres`` requires a reachable, migrated database and pins
    ``storage_backend``, so a single ``pytest`` invocation exercises both.
    """
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


# --------------------------------------------------------------------------- #
# the nail                                                                     #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("active_backend", ["memory", "postgres"], indirect=True)
async def test_interrupt_stale_running_rewrites_only_non_terminal_rows(active_backend):
    """A leftover ``running`` row becomes ``interrupted``; a terminal row does not.

    ``postgres`` is the discriminating parameter: before the fix the aggregate
    ``RETURNING`` raised ``GroupingError`` out of this call (there is no broad
    ``except`` around the store method itself), so the assertions below are only
    reachable once the statement is valid.
    """
    store = get_workspace_store()
    # The factory must honour the declared profile — otherwise a silent
    # "always memory" regression would make this nail vacuous.
    expected = PgWorkspaceStore if active_backend == "postgres" else MemoryWorkspaceStore
    assert isinstance(store, expected), (
        f"active_backend={active_backend!r} must drive get_workspace_store() to "
        f"{expected.__name__}, got {type(store).__name__}"
    )

    suffix = _suffix()
    tenant = f"t-inc32-interrupt-{suffix}"
    stale_id = f"run-stale-{suffix}"  # non-terminal → must be rewritten
    done_id = f"run-done-{suffix}"  # terminal → must be left alone

    await store.save(
        WorkspaceRunRecord(
            run_id=stale_id,
            tenant_id=tenant,
            session_id=stale_id,
            intent="崩溃前仍在跑的任务",
            status="running",
        )
    )
    await store.save(
        WorkspaceRunRecord(
            run_id=done_id,
            tenant_id=tenant,
            session_id=done_id,
            intent="已经完成的任务",
            status="completed",
        )
    )

    # Sanity: the stale row really starts non-terminal.
    seeded = await store.get(tenant, stale_id)
    assert seeded is not None and seeded.status == "running"

    count = await store.interrupt_stale_running()

    # 1. the reconciliation reports real work (effect, not merely "no raise").
    assert count >= 1
    # 2. the stale row is *actually* rewritten to ``interrupted`` …
    after = await store.get(tenant, stale_id)
    assert after is not None
    assert after.status == INTERRUPTED_STATUS
    # 3. … and the already-terminal row is untouched.
    done = await store.get(tenant, done_id)
    assert done is not None
    assert done.status == "completed"
