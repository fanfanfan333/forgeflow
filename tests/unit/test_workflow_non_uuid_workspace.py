"""Workflow detail/trace with a non-UUID workspace claim.

Regression for the local c9015cb acceptance finding: a sales workflow
completed, but ``GET /workflows/{run_id}`` and ``/{run_id}/trace``
returned 400 because the default tenant value (``"default"``) is not a
UUID. The write path already stored such runs with ``workspace_id IS
NULL`` via ``_legacy_workspace_uuid``; the read path passed the raw
string to the UUID column instead, so the driver raised and the
handler wrapped it as a spurious 400.

The fake pool below mimics that driver behaviour: a string workspace
parameter raises, a UUID parameter or an ``IS NULL`` query succeeds.
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.dependencies import get_current_user, get_pool, get_workspace_id
from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.routers import workflows
from forgeflow.rbac.models import UserContext

RUN_ID = str(uuid.uuid4())
THREAD_ID = str(uuid.uuid4())
WORKSPACE_UUID = str(uuid.uuid4())


class _FakeConn:
    def __init__(self, row: dict, traces: list[dict]) -> None:
        self.row = row
        self.traces = traces
        self.calls: list[tuple] = []

    async def fetchrow(self, query: str, *args):  # type: ignore[no-untyped-def]
        self.calls.append((query, args))
        if "workspace_id = $2" in query:
            # asyncpg would reject a non-UUID string for a UUID column.
            if isinstance(args[1], str):
                raise ValueError("invalid input for uuid: 'default'")
            return self.row if args[1] == uuid.UUID(WORKSPACE_UUID) else None
        return self.row

    async def fetch(self, query: str, *args):  # type: ignore[no-untyped-def]
        self.calls.append((query, args))
        return self.traces


class _FakeAcquire:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self.conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakePool:
    def __init__(self, row: dict, traces: list[dict]) -> None:
        self.conn = _FakeConn(row, traces)

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self.conn)


def _row() -> dict:
    return {
        "id": uuid.UUID(RUN_ID),
        "thread_id": uuid.UUID(THREAD_ID),
        "status": "completed",
        "metadata": {"final_stage": "done"},
        "total_tokens": 10,
        "total_cost_usd": 0.0,
        "created_at": "2026-10-29T00:00:00+00:00",
        "completed_at": "2026-10-29T00:01:00+00:00",
        "input_data": {},
        "user_id": "admin-1",
    }


def _client(pool: _FakePool, workspace: str | None) -> TestClient:
    app = FastAPI()
    app.dependency_overrides[resolve_tenant] = lambda: "tenant-test"
    app.dependency_overrides[get_pool] = lambda: pool
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="admin-1", role="admin"
    )
    app.dependency_overrides[get_workspace_id] = lambda: workspace
    app.include_router(workflows.router, prefix="/prefix")
    return TestClient(app, raise_server_exceptions=False)


def test_detail_with_default_workspace_falls_back_to_legacy_null_rows():
    pool = _FakePool(_row(), [])
    response = _client(pool, "default").get(f"/prefix/{RUN_ID}")
    assert response.status_code == 200, response.text
    assert response.json()["run_id"] == RUN_ID
    assert "IS NULL" in pool.conn.calls[0][0]
    assert not any(
        isinstance(arg, str) and arg == "default"
        for _query, args in pool.conn.calls
        for arg in args
    )


def test_trace_with_default_workspace_falls_back_to_legacy_null_rows():
    traces = [
        {
            "id": uuid.uuid4(),
            "agent_name": "researcher",
            "stage": "research",
            "started_at": "2026-10-29T00:00:00+00:00",
            "completed_at": "2026-10-29T00:00:30+00:00",
            "tokens_used": 5,
            "cost_usd": 0.0,
            "error": None,
            "output_patch": {},
        }
    ]
    pool = _FakePool(_row(), traces)
    response = _client(pool, "default").get(f"/prefix/{RUN_ID}/trace")
    assert response.status_code == 200, response.text
    assert len(response.json()) == 1
    assert response.json()[0]["agent_name"] == "researcher"


def test_detail_with_uuid_workspace_still_filters_by_that_workspace():
    pool = _FakePool(_row(), [])
    response = _client(pool, WORKSPACE_UUID).get(f"/prefix/{RUN_ID}")
    assert response.status_code == 200, response.text
    query, args = pool.conn.calls[0]
    assert "workspace_id = $2" in query
    assert args[1] == uuid.UUID(WORKSPACE_UUID)


def test_detail_with_invalid_run_id_is_still_400():
    pool = _FakePool(_row(), [])
    response = _client(pool, "default").get("/prefix/not-a-uuid")
    assert response.status_code == 400
