"""INC42 — workspace history lifecycle pins (``_inc42_brief.md`` T10).

Six defect-driven groups, each pinning a ruling so it cannot silently regress:

  1. **Soft delete, not hard** (Q4=A + Q5=B) — deleting a session hides it from
     *every* read path, but the row is **kept** (``deleted_at`` set), so the audit
     chain survives. Deleting an already-deleted session marks nothing.
  2. **Tenant isolation** — a delete is scoped by ``tenant_id``; a cross-tenant
     call can never touch another tenant's session (``deleted == 0`` ⇒ 404).
  3. **RBAC** (Q6=A) — ``("DELETE","/workspace")`` maps to ``("execute",
     "workflows")`` (the same gate as ``canExecute``), so a ``viewer`` is 403 at
     the middleware and an anonymous caller 401; a holder passes through.
  4. **runtime_mode round-trip** (defect ④) — ``GET /runs/{id}`` returns the
     recorded mode **verbatim**; a record whose mode was never recorded returns
     ``""`` (the honest "not recorded"), *never* a fabricated ``"deterministic"``
     that would make the result page lie 「未连接模型服务」.
  5. **Migration 017 is additive + re-entrant** — chained after 016, every
     ``ADD COLUMN`` / ``CREATE INDEX`` guarded with ``IF NOT EXISTS``, the upgrade
     body drops nothing, and the downgrade is symmetric (mirrors
     ``test_migration_011.py``).
  6. **Frontend vocabulary spike** (Q7 + defect ③/④) — the home ``TaskRow``
     delegates status to ``realRun.runStatusMeta`` (no hand-written ternary), and
     ``runStatusMeta`` really colours ``aborted``/``interrupted`` amber; the
     disclaimer gate in ``deriveDegradeNotice`` only fires on an **explicit**
     ``deterministic`` mode. Each source parser carries a positive control so a
     vacuous pass (parsing nothing / the wrong text) is impossible.

Citation discipline: ``file::symbol`` anchors, never ``file:line``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import re
from types import ModuleType

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.routers import runs as runs_router
from forgeflow.api.routers import workspace as workspace_router
from forgeflow.auth.jwt import create_access_token
from forgeflow.middleware.auth import RBACMiddleware
from forgeflow.observability.metrics_source import PostgresMetricsSource
from forgeflow.rbac.enforcer import RBACEnforcer
from forgeflow.rbac.policies import ROUTE_PERMISSION_MAP
from forgeflow.runtime.dispatcher import reset_run_dispatcher
from forgeflow.runtime.orchestrator import (
    RunRecord,
    get_run_store,
    reset_run_store,
)
from forgeflow.workspace import store as ws_store
from forgeflow.workspace.models import WorkspaceRunRecord
from forgeflow.workspace.store import get_workspace_store, reset_workspace_store

TENANT = "t-inc42"
OTHER_TENANT = "t-inc42-other"
_ENFORCER = RBACEnforcer()

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "alembic" / "versions"
_TS_HOME = REPO_ROOT / "frontend/src/views/HomeView.tsx"
_TS_REALRUN = REPO_ROOT / "frontend/src/views/runs/realRun.ts"
_TS_OVERVIEW = REPO_ROOT / "frontend/src/views/OverviewView.tsx"


@pytest.fixture(autouse=True)
def _clean_state():
    """Deterministic under any ``STORAGE_BACKEND`` — reset every in-process store."""
    reset_run_store()
    reset_run_dispatcher()
    reset_workspace_store()
    yield
    reset_run_store()
    reset_run_dispatcher()
    reset_workspace_store()


@pytest.fixture(autouse=True)
def _clean_workspace_rows():
    """Clear *this file's* ``workspace_runs`` rows before each case (PG only).

    The postgres store is a persistent table: rows written by an earlier case —
    or by an earlier run of the suite — survive into the next one, so a fixed
    ``run_id`` / ``session_id`` collides with its own soft-deleted remains
    (``save``'s ``ON CONFLICT DO UPDATE`` deliberately never clears
    ``deleted_at``, because production ``run_id``s are uuids and never reused).
    The memory profile never sees this because ``reset_workspace_store`` empties
    ``_STORE`` per test; this fixture gives the postgres profile the same clean
    slate, so the file is a *deterministic* gate under either backend.

    Scope is deliberately *narrow*: only the two tenants this file owns are
    purged — a whole-table ``DELETE`` would destroy the shared dev DB's other
    tenants (``default`` and friends hold real runs). It uses an independent
    psycopg short connection — the same idiom as ``tests/conftest.py::pg_purge``
    — so it never touches the module-global asyncpg pool and cannot cross event
    loops. A no-op under the memory profile.
    """
    from forgeflow.config import get_settings

    if get_settings().storage_backend.lower() == "postgres":
        import psycopg

        dsn = get_settings().postgres_sync_url.replace(
            "postgresql+psycopg://", "postgresql://"
        )
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM workspace_runs WHERE tenant_id IN (%s, %s)",
                    (TENANT, OTHER_TENANT),
                )
            conn.commit()
    yield


# =========================================================================== #
# Shared helpers                                                              #
# =========================================================================== #
def _store():
    return get_workspace_store()


def _run(coro):
    """Drive one async store call from a sync test (fresh loop each call).

    ``forgeflow.database`` keeps a single *module-global* asyncpg pool pinned to
    the event loop that created it. ``asyncio.run`` builds and closes a fresh
    loop on every call, so a pool left behind by an earlier call would be reused
    from that now-closed loop (``RuntimeError: Event loop is closed`` /
    ``asyncpg`` ``InterfaceError: another operation is in progress``). Drop the
    global before *and* after so no consumer can ever acquire a connection from
    a pool pinned to a dead loop — the invariant ``conftest``'s
    ``_isolate_asyncpg_pool`` upholds per test, applied here at the finer
    per-call granularity a multi-``_run`` test needs.
    """
    import forgeflow.database as _db

    _db._pool = None
    try:
        return asyncio.run(coro)
    finally:
        _db._pool = None


def _record(
    run_id: str,
    session_id: str,
    *,
    tenant: str = TENANT,
    status: str = "completed",
    runtime_mode: str = "",
    created_at: str = "2026-10-05T00:00:01+00:00",
) -> WorkspaceRunRecord:
    return WorkspaceRunRecord(
        run_id=run_id,
        tenant_id=tenant,
        session_id=session_id,
        intent="分析销售数据并生成报告",
        title="分析销售数据并生成报告",
        status=status,
        outcome="success",
        runtime_mode=runtime_mode,
        created_at=created_at,
        updated_at=created_at,
    )


def _seed(run_id: str, session_id: str, *, tenant: str = TENANT) -> None:
    _run(_store().save(_record(run_id, session_id, tenant=tenant)))


def _live_session_ids(tenant: str = TENANT) -> set[str]:
    return {g["session_id"] for g in _run(_store().list_sessions(tenant))}


def _soft_deleted_flags(
    run_ids: tuple[str, ...], *, tenant: str = TENANT
) -> dict[str, bool]:
    """Whether each ``run_id`` row is soft-deleted, read from the store in use.

    The public read paths all filter ``deleted_at IS NULL``, so the "the row is
    kept" pin has to look at storage directly. Memory profile: the module-global
    ``_STORE`` dict. Postgres profile: a direct psycopg query — an independent
    short connection, never the module-global asyncpg pool. Same intent and
    strength either way: one marker flag per ``run_id``.
    """
    from forgeflow.config import get_settings

    if get_settings().storage_backend.lower() == "postgres":
        import psycopg

        dsn = get_settings().postgres_sync_url.replace(
            "postgresql+psycopg://", "postgresql://"
        )
        placeholders = ", ".join(["%s"] * len(run_ids))
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT run_id, deleted_at FROM workspace_runs "
                    f"WHERE tenant_id = %s AND run_id IN ({placeholders})",
                    (_store().scope_key(tenant), *run_ids),
                )
                rows = {
                    str(rid): (deleted is not None)
                    for rid, deleted in cur.fetchall()
                }
        return {rid: rows[rid] for rid in run_ids}

    bucket = ws_store._STORE[_store().scope_key(tenant)]
    return {rid: bucket[rid].deleted_at is not None for rid in run_ids}


class _PoolSafeTestClient(TestClient):
    """A ``TestClient`` that never lets the global pool outlive an event loop.

    Starlette's ``TestClient`` spins up a **fresh blocking portal (its own event
    loop) for every request** when it is not entered as a context manager — and
    these tests build a bare ``TestClient(app)`` and call ``client.get/delete``.
    ``forgeflow.database`` keeps one *module-global* asyncpg pool pinned to the
    loop that built it, so a pool created while serving request *N* would be
    reused — from its now-closed loop — by request *N+1* (and by a later
    ``_run`` call), yielding ``asyncpg`` ``InterfaceError: another operation is
    in progress``. Drop the global around every request so each one builds its
    pool on the loop actually running it.
    """

    def request(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        import forgeflow.database as _db

        _db._pool = None
        try:
            return super().request(*args, **kwargs)
        finally:
            _db._pool = None


def _workspace_client(tenant: str, *, rbac: bool = False) -> TestClient:
    app = FastAPI()
    if rbac:
        app.add_middleware(RBACMiddleware)
    app.dependency_overrides[resolve_tenant] = lambda: tenant
    app.include_router(workspace_router.router, prefix="/workspace")
    return _PoolSafeTestClient(app)


def _runs_client(tenant: str) -> TestClient:
    app = FastAPI()
    app.dependency_overrides[resolve_tenant] = lambda: tenant
    app.include_router(runs_router.router, prefix="/runs")
    return _PoolSafeTestClient(app)


def _token(role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user_id=f'{role}-1', role=role)}"}


# =========================================================================== #
# 1. Soft delete hides the session but KEEPS the row (Q4=A + Q5=B)            #
# =========================================================================== #
def test_soft_delete_hides_from_every_read_path_but_keeps_the_row():
    store = _store()
    _run(store.save(_record("run-1", "s-1", created_at="2026-10-05T00:00:01+00:00")))
    _run(store.save(_record("run-2", "s-1", created_at="2026-10-05T00:00:02+00:00")))
    _run(store.save(_record("run-3", "s-2", created_at="2026-10-05T00:00:03+00:00")))

    # Live before the delete.
    assert _live_session_ids() == {"s-1", "s-2"}
    assert _run(store.get(TENANT, "run-1")) is not None

    marked = _run(store.soft_delete_session(TENANT, "s-1"))
    assert marked == 2

    # Hidden from every read path...
    assert _live_session_ids() == {"s-2"}
    assert _run(store.get(TENANT, "run-1")) is None
    assert _run(store.list_session_runs(TENANT, "s-1")) == []
    assert {r.run_id for r in _run(store.list_recent_for_tenant(TENANT))} == {"run-3"}

    # ...but the ROW is kept (audit chain), only marked. The marker is not
    # exposed by the public read paths (they all filter ``deleted_at IS NULL``),
    # so read it from whichever store is active — memory ``_STORE`` or a direct
    # postgres query (see ``_soft_deleted_flags``).
    flags = _soft_deleted_flags(("run-1", "run-2", "run-3"))
    assert flags["run-1"] is True
    assert flags["run-2"] is True
    assert flags["run-3"] is False

    # Idempotent: a second delete marks nothing (already gone).
    assert _run(store.soft_delete_session(TENANT, "s-1")) == 0


def test_positive_control_soft_deleted_flag_reader_can_go_red():
    """The backend-agnostic row-marker reader is a real discriminator.

    Proves ``_soft_deleted_flags`` — the mechanism behind the "the row is kept"
    pin in ``test_soft_delete_hides_from_every_read_path_but_keeps_the_row`` —
    actually flips between a live and a soft-deleted row, so that pin cannot
    pass vacuously (e.g. if the reader always reported ``False``).
    """
    store = _store()
    _run(store.save(_record("pc-1", "s-pc", created_at="2026-10-05T00:00:01+00:00")))
    _run(store.save(_record("pc-2", "s-pc", created_at="2026-10-05T00:00:02+00:00")))

    live = _soft_deleted_flags(("pc-1", "pc-2"))
    assert live == {"pc-1": False, "pc-2": False}

    _run(store.soft_delete_session(TENANT, "s-pc"))

    deleted = _soft_deleted_flags(("pc-1", "pc-2"))
    assert deleted == {"pc-1": True, "pc-2": True}
    # Real discriminator: the post-delete reading differs from the pre-delete
    # one, so the "row is kept" pin *can* go red (invert the expectation and the
    # same assertions fail).
    assert deleted != live


def test_delete_session_endpoint_returns_200_then_404():
    _seed("run-i", "s-i")
    client = _workspace_client(TENANT)
    first = client.delete("/workspace/sessions/s-i")
    assert first.status_code == 200, first.text
    assert first.json() == {"session_id": "s-i", "deleted": 1}
    # The session detail read is a not-found once soft-deleted.
    assert client.get("/workspace/sessions/s-i").status_code == 404
    # A repeat delete is honestly 404 (deleted == 0), never a fake success.
    assert client.delete("/workspace/sessions/s-i").status_code == 404


def test_delete_unknown_session_is_404():
    assert _workspace_client(TENANT).delete("/workspace/sessions/nope").status_code == 404


# =========================================================================== #
# 2. Tenant isolation — a delete never crosses tenants                        #
# =========================================================================== #
def test_soft_delete_is_tenant_scoped_in_the_store():
    store = _store()
    _run(store.save(_record("x-1", "s-x", tenant=OTHER_TENANT)))
    # A TENANT delete must not see / touch OTHER_TENANT's session.
    assert _run(store.soft_delete_session(TENANT, "s-x")) == 0
    assert _run(store.get(OTHER_TENANT, "x-1")) is not None


def test_delete_is_tenant_scoped_over_http():
    _seed("run-o", "s-o", tenant=OTHER_TENANT)
    resp = _workspace_client(TENANT).delete("/workspace/sessions/s-o")
    assert resp.status_code == 404, resp.text
    # The owning tenant still sees it.
    assert "s-o" in _live_session_ids(OTHER_TENANT)


# =========================================================================== #
# 3. RBAC — delete history = execute:workflows (Q6=A)                          #
# =========================================================================== #
def test_delete_route_permission_is_mapped_to_execute_workflows():
    assert ROUTE_PERMISSION_MAP[("DELETE", "/workspace")] == ("execute", "workflows")
    # Longest-prefix match resolves the real delete path to the same gate.
    assert RBACMiddleware._resolve_permission("DELETE", "/workspace/sessions/s-1") == (
        "execute",
        "workflows",
    )


def test_can_execute_roles_may_delete_viewers_may_not():
    assert _ENFORCER.check("manager", "execute", "workflows")
    assert _ENFORCER.check("admin", "execute", "workflows")
    assert _ENFORCER.check("sales_rep", "execute", "workflows")
    assert not _ENFORCER.check("viewer", "execute", "workflows")
    assert not _ENFORCER.check("anonymous", "execute", "workflows")


def test_unauthenticated_delete_is_401():
    assert _workspace_client(TENANT, rbac=True).delete("/workspace/sessions/s-x").status_code == 401


def test_viewer_delete_is_403_and_the_session_survives():
    _seed("run-v", "s-v")
    resp = _workspace_client(TENANT, rbac=True).delete(
        "/workspace/sessions/s-v", headers=_token("viewer")
    )
    assert resp.status_code == 403, resp.text
    # Refused before the handler ⇒ the session is still live.
    assert "s-v" in _live_session_ids()


def test_manager_delete_passes_the_middleware_and_soft_deletes():
    _seed("run-m", "s-m")
    resp = _workspace_client(TENANT, rbac=True).delete(
        "/workspace/sessions/s-m", headers=_token("manager")
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"session_id": "s-m", "deleted": 1}
    assert "s-m" not in _live_session_ids()


# =========================================================================== #
# 4. runtime_mode round-trips verbatim (defect ④)                             #
# =========================================================================== #
def _terminal_run_record(
    run_id: str, *, runtime_mode: str = "deterministic", tenant: str = TENANT
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        thread_id=f"{run_id}-th",
        tenant_id=tenant,
        agent_id=None,
        intent="分析销售数据并生成报告",
        status="completed",
        outcome="success",
        steps=[],
        errors=[],
        created_at="2026-10-05T00:00:00+00:00",
        completed_at="2026-10-05T00:00:01+00:00",
        runtime_mode=runtime_mode,
        session_id=run_id,
    )


def test_react_runtime_mode_round_trips_through_get_run():
    get_run_store().save(_terminal_run_record("run-react", runtime_mode="react"))
    resp = _runs_client(TENANT).get("/runs/run-react")
    assert resp.status_code == 200, resp.text
    assert resp.json()["runtime_mode"] == "react"


def test_unrecorded_runtime_mode_stays_empty_and_is_never_deterministic():
    # A pre-017 row: the executor was never recorded (""), NOT deterministic.
    get_run_store().save(_terminal_run_record("run-hist", runtime_mode=""))
    resp = _runs_client(TENANT).get("/runs/run-hist")
    assert resp.status_code == 200, resp.text
    assert resp.json()["runtime_mode"] == ""  # honest "not recorded"


# =========================================================================== #
# 5. Migration 017 — additive, re-entrant, chained after 016                  #
# =========================================================================== #
def _load_migration(filename: str, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, MIGRATIONS / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_source(filename: str) -> str:
    return (MIGRATIONS / filename).read_text(encoding="utf-8")


_017 = "017_workspace_history_lifecycle.py"
_016 = "016_workspace_runs.py"


def test_017_revision_chain():
    mig = _load_migration(_017, "mig_017_under_test")
    assert mig.revision == "017"
    assert mig.down_revision == "016"
    assert callable(mig.upgrade)
    assert callable(mig.downgrade)


def test_017_shifts_head_and_leaves_016_untouched():
    m16 = _load_migration(_016, "mig_016_under_test_inc42")
    assert m16.revision == "016"
    assert m16.down_revision == "015"
    m17 = _load_migration(_017, "mig_017_under_test_inc42")
    assert m17.down_revision == m16.revision


def test_017_adds_expected_columns():
    source = _migration_source(_017)
    for column in ("deleted_at", "runtime_mode", "llm"):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in source, column


def test_017_is_reentrant_and_drops_nothing_in_upgrade():
    source = _migration_source(_017)
    upgrade = source.split("def upgrade()", 1)[1].split("def downgrade()", 1)[0]
    # Every ADD COLUMN / CREATE INDEX in the upgrade body is guarded with IF NOT EXISTS.
    assert re.search(r"ADD COLUMN(?! IF NOT EXISTS)", upgrade) is None
    assert re.search(r"CREATE (TABLE|INDEX)(?! IF NOT EXISTS)", upgrade) is None
    assert "DROP " not in upgrade


def test_017_downgrade_is_symmetric():
    source = _migration_source(_017)
    downgrade = source.split("def downgrade()", 1)[1]
    for column in ("deleted_at", "runtime_mode", "llm"):
        assert f"DROP COLUMN IF EXISTS {column}" in downgrade


# =========================================================================== #
# 6. Frontend vocabulary spike (Q7 + defect ③/④)                             #
# =========================================================================== #
def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def _region(src: str, startswith: str) -> str:
    """The verbatim function text — from ``startswith`` to its column-0 brace."""
    lines = src.split("\n")
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith(startswith))
    except StopIteration:  # pragma: no cover — only if the function is renamed
        raise AssertionError(f"{startswith!r} not found in source")
    for i in range(start + 1, len(lines)):
        if lines[i] == "}":  # a top-level (unindented) closing brace
            return "\n".join(lines[start : i + 1])
    raise AssertionError(f"{startswith!r} function is not terminated at column 0")


def _code_only(src: str) -> str:
    """Strip ``//`` line comments and ``{/* … */}`` JSX comments (code only)."""
    src = re.sub(r"\{/\*.*?\*/\}", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


# The status words the shared vocabulary owns — HomeView must not re-hard-code them.
_STATUS_WORDS = ("进行中", "已完成", "已中止", "已中断")


def test_home_taskrow_delegates_status_to_the_shared_vocabulary():
    """defect ③ — ``TaskRow`` must call ``runStatusMeta``, not a hand-written ternary."""
    region = _region(_read(_TS_HOME), "function TaskRow(")
    code = _code_only(region)
    assert "runStatusMeta(" in code, "TaskRow must delegate to runStatusMeta(run.status)"
    leaked = [w for w in _STATUS_WORDS if w in code]
    assert leaked == [], f"TaskRow re-hard-codes status vocabulary: {leaked}"


def test_positive_control_home_detector_catches_a_handwritten_status_ternary():
    """Injects the exact defect ③ shape and proves the detector flags it."""
    code = _code_only(_region(_read(_TS_HOME), "function TaskRow("))
    drifted = code.replace(
        "runStatusMeta(run.status)",
        "run.status === 'running' ? '进行中' : '已完成'",
        1,
    )
    assert drifted != code, "fixture drift: the runStatusMeta(run.status) call moved"
    assert any(w in drifted for w in _STATUS_WORDS)


_ARM_RE = re.compile(
    r"if \((?P<cond>.*?)\) return \{ label: '(?P<label>[^']*)', tone: '(?P<tone>[^']*)' \}"
)


def _runstatusmeta_arms(src: str) -> list[tuple[str, str, str]]:
    region = _region(src, "export function runStatusMeta")
    return [
        (m.group("cond"), m.group("label"), m.group("tone"))
        for m in _ARM_RE.finditer(region)
    ]


def _arm_for(arms: list[tuple[str, str, str]], token: str) -> tuple[str, str] | None:
    for cond, label, tone in arms:
        if token in cond:
            return label, tone
    return None


def test_runstatusmeta_colours_aborted_and_interrupted_amber_and_running_blue():
    """Q7 — the ONE status vocabulary; the tones the badge consumes."""
    arms = _runstatusmeta_arms(_read(_TS_REALRUN))
    assert arms, "parsed no runStatusMeta arms (parser vacuous / function moved)"
    assert _arm_for(arms, "'run'") == ("进行中", "blue")  # was emerald (bug)
    assert _arm_for(arms, "'complete'") == ("已完成", "emerald")  # was blue (bug)
    assert _arm_for(arms, "'abort'") == ("已中止", "amber")  # defect ③: not "进行中"
    assert _arm_for(arms, "'interrupt'") == ("已中断", "amber")


def test_positive_control_runstatusmeta_detector_catches_a_wrong_tone():
    region = _region(_read(_TS_REALRUN), "export function runStatusMeta")
    drifted = region.replace("'已中止', tone: 'amber'", "'已中止', tone: 'red'", 1)
    assert drifted != region, "fixture drift: the aborted arm moved"
    arms = [
        (m.group("cond"), m.group("label"), m.group("tone"))
        for m in _ARM_RE.finditer(drifted)
    ]
    assert not any("'abort'" in c and t == "amber" for c, _, t in arms)


def test_degrade_notice_gates_env_on_explicit_deterministic_only():
    """defect ④ — the 「未连接模型服务」 notice may NOT key off "any non-model mode"."""
    code = _code_only(_region(_read(_TS_REALRUN), "export function deriveDegradeNotice"))
    assert "mode === 'deterministic'" in code
    # The old, over-broad fallback (unknown / empty mode ⇒ "未连接模型") must be gone.
    assert "!isModelDriven(mode)" not in code


def test_positive_control_degrade_gate_detects_the_old_over_broad_condition():
    code = _code_only(_region(_read(_TS_REALRUN), "export function deriveDegradeNotice"))
    drifted = code.replace(
        "mode === 'deterministic' && !isModelDriven(mode, llm)",
        "!isModelDriven(mode)",
        1,
    )
    assert drifted != code, "fixture drift: the deterministic gate moved"
    assert "!isModelDriven(mode)" in drifted


# =========================================================================== #
# 7. FIX-1 — a soft delete must invalidate the process-local run cache        #
# =========================================================================== #
def _memory_run(run_id: str, session_id: str, *, tenant: str = TENANT) -> RunRecord:
    """A terminal ``RunRecord`` for the **in-process** ``MemoryRunStore``.

    The store here is the one ``GET /runs`` / ``GET /runs/{id}`` read directly —
    distinct from ``workspace_runs`` (the persisted header). ``session_id`` is a
    parameter so a session can own more than one run.
    """
    return RunRecord(
        run_id=run_id,
        thread_id=f"{run_id}-th",
        tenant_id=tenant,
        agent_id=None,
        intent="分析销售数据并生成报告",
        status="completed",
        outcome="success",
        steps=[],
        errors=[],
        created_at="2026-10-05T00:00:00+00:00",
        completed_at="2026-10-05T00:00:01+00:00",
        runtime_mode="deterministic",
        session_id=session_id,
    )


def test_fix1_delete_session_invalidates_the_in_process_run_cache():
    """The QA minimal reproduction: after ``DELETE``, all three read paths agree.

    A run that exists BOTH in the process-local store (this process created it)
    and in ``workspace_runs`` (its persisted header) must vanish from the session
    list, from ``get_run_store().get`` (⇒ ``GET /runs/{id}`` 404) and from
    ``GET /runs``. Before FIX-1 the store kept serving it, so it "resurrected".
    """
    get_run_store().save(_memory_run("run-1", "s-1"))
    _seed("run-1", "s-1")
    assert get_run_store().get("run-1") is not None
    assert "s-1" in _live_session_ids()

    resp = _workspace_client(TENANT).delete("/workspace/sessions/s-1")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"session_id": "s-1", "deleted": 1}

    assert "s-1" not in _live_session_ids()  # list_sessions hides it
    assert get_run_store().get("run-1") is None  # GET /runs/{id} ⇒ 404
    items = _runs_client(TENANT).get("/runs").json()["items"]
    assert "run-1" not in {it["run_id"] for it in items}  # GET /runs hides it


def test_fix1_deleted_zero_does_not_touch_the_in_process_store():
    """A 404 delete (``deleted == 0``) must NOT discard an unrelated run.

    The session does not exist in ``workspace_runs`` ⇒ the route returns 404 and
    must return **before** touching the run cache.
    """
    get_run_store().save(_memory_run("run-x", "s-x"))
    resp = _workspace_client(TENANT).delete("/workspace/sessions/s-x")
    assert resp.status_code == 404, resp.text
    assert get_run_store().get("run-x") is not None


def test_fix1_discard_session_removes_by_session_and_by_run_id():
    """The store primitive itself: session match AND ``run_id == session_id``."""
    store = get_run_store()
    store.save(_memory_run("s-1", "s-1"))  # first run of a session reuses its run_id
    store.save(_memory_run("run-2", "s-1"))  # a later run shares the session_id
    store.save(_memory_run("run-3", "s-2"))  # a different session — untouched

    assert store.discard_session("s-1") == 2
    assert store.get("s-1") is None
    assert store.get("run-2") is None
    assert store.get("run-3") is not None
    # Idempotent + an empty id discards nothing (never a mass wipe).
    assert store.discard_session("s-1") == 0
    assert store.discard_session("") == 0
    assert store.get("run-3") is not None


# =========================================================================== #
# 8. FIX-2 — the PG metrics reads must exclude soft-deleted rows              #
# =========================================================================== #
def _norm_sql(sql: object) -> str:
    """Whitespace-normalised, lower-cased SQL text for a substring check."""
    return " ".join(str(sql).split()).lower()


def _capturing_conn(pool):
    """The mock ``conn`` the ``mock_pool`` fixture hands to ``async with``."""
    return pool.acquire.return_value.__aenter__.return_value


def test_fix2_workspace_summary_excludes_soft_deleted(mock_pool):
    """``_workspace_summary``'s ``total_runs`` must ignore ``deleted_at`` rows.

    Drives the **real** method and captures the SQL it emits (stronger than a
    source grep, weaker than a live PG query — no PG in the unit profile).
    """
    src = PostgresMetricsSource(pool=mock_pool)
    _run(src._workspace_summary(mock_pool, TENANT))
    sql = _norm_sql(_capturing_conn(mock_pool).fetchrow.call_args.args[0])
    assert "from workspace_runs" in sql
    assert "deleted_at is null" in sql
    # Positive control — the predicate check is not vacuous: stripping the clause
    # from the captured SQL makes the same assertion fail.
    assert "deleted_at is null" not in sql.replace("and deleted_at is null", "")


def test_fix2_workspace_recent_excludes_soft_deleted(mock_pool):
    """``_workspace_recent`` must not return a soft-deleted run either."""
    src = PostgresMetricsSource(pool=mock_pool)
    _run(src._workspace_recent(mock_pool, TENANT, 5))
    sql = _norm_sql(_capturing_conn(mock_pool).fetch.call_args.args[0])
    assert "from workspace_runs" in sql
    assert "deleted_at is null" in sql
    assert "deleted_at is null" not in sql.replace("and deleted_at is null", "")


def test_fix2_positive_control_captured_sql_detector_can_go_red(mock_pool):
    """Proves the FIX-2 detector flips: a query WITHOUT the clause is flagged."""
    src = PostgresMetricsSource(pool=mock_pool)
    _run(src._workspace_summary(mock_pool, TENANT))
    drifted = _norm_sql(_capturing_conn(mock_pool).fetchrow.call_args.args[0]).replace(
        "and deleted_at is null", ""
    )
    assert "deleted_at is null" not in drifted  # the detector would now FAIL


# =========================================================================== #
# 9. FIX-3 — OverviewView delegates to the ONE status vocabulary (Q7)         #
# =========================================================================== #
def test_fix3_overview_statusbadge_delegates_to_the_shared_vocabulary():
    """``running`` renders 「进行中」 via ``runStatusMeta`` (no hand-written drift).

    ``runStatusMeta('running')`` == 「进行中」/blue is pinned by
    ``test_runstatusmeta_colours_aborted_and_interrupted_amber_and_running_blue``;
    here we pin that ``statusBadge`` actually calls it and no longer hard-codes
    the drifted word 「运行中」.
    """
    code = _code_only(_region(_read(_TS_OVERVIEW), "function statusBadge("))
    assert "runStatusMeta(" in code
    assert "meta.label" in code and "meta.tone" in code
    assert "运行中" not in code


def test_fix3_overview_keeps_done_success_paused_words():
    """Red line: done / success / paused must NOT drift into 「其他状态」."""
    code = _code_only(_region(_read(_TS_OVERVIEW), "function statusBadge("))
    assert "已完成" in code  # done / completed / success
    assert "等待审批" in code  # pending_approval / awaiting_approval / paused
    # The honest fallback for a token the vocabulary does not know is retained.
    assert "statusLabel(" in code


def test_positive_control_fix3_detector_catches_the_drifted_running_word():
    code = _code_only(_region(_read(_TS_OVERVIEW), "function statusBadge("))
    drifted = code.replace("meta.label", "运行中", 1)
    assert drifted != code, "fixture drift: the meta.label delegation moved"
    assert "运行中" in drifted  # the detector would flag this drift


# =========================================================================== #
# 10. FIX-4 — `rejected` → 已拒绝/amber is a DELIBERATE ruling (pinned)        #
# =========================================================================== #
def test_fix4_runstatusmeta_pins_rejected_as_deliberate_amber():
    """Task-book T5's "其余→中性灰" does NOT apply to ``rejected``.

    Ruling: 「已拒绝」 is amber on purpose — a rejected run is not a crash (that
    would be red) but is not finished either, so it sits with aborted/interrupted.
    Pinned so a future "tidy-up" cannot silently reassign it.
    """
    arms = _runstatusmeta_arms(_read(_TS_REALRUN))
    assert arms, "parsed no runStatusMeta arms (parser vacuous / function moved)"
    assert _arm_for(arms, "'reject'") == ("已拒绝", "amber")
