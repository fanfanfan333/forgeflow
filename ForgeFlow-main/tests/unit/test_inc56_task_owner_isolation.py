"""INC56 — a task is visible only to the user who initiated it.

The platform already had *tenant* isolation. This file pins the stricter
default the multi-agent upgrade requires: **within one tenant**, another user
must not be able to list, read, stream, download, stop, delete or attach to
someone else's task. Every refusal is a 404 (not 403) so the response cannot
confirm that the guessed id exists.

These are black-box route tests: the real routers are mounted with pinned
identity dependencies, and the only assertions are on the user-visible
HTTP/status/store result.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.dependencies import get_current_user
from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.routers import run_outcomes, runs, workspace
from forgeflow.rbac.models import UserContext
from forgeflow.runtime.orchestrator import RunRecord, get_run_store, reset_run_store
from forgeflow.workspace.models import WorkspaceRunRecord
from forgeflow.workspace.store import get_workspace_store, reset_workspace_store


TENANT = "tenant-inc56"
OWNER = "owner-inc56"
OTHER = "other-inc56"


def _record(run_id: str = "run-inc56", *, actor: str = OWNER) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        thread_id="thread-inc56",
        tenant_id=TENANT,
        agent_id=None,
        intent="inspect a private task",
        status="running",
        outcome="pending",
        steps=[],
        errors=[],
        created_at="2026-10-28T00:00:00+00:00",
        actor_user_id=actor,
        session_id="session-inc56",
        artifacts=[
            {"id": "secret.txt", "format": "text", "content": "private artifact"}
        ],
    )


def _client(router, *, user_id: str) -> TestClient:
    app = FastAPI()
    app.dependency_overrides[resolve_tenant] = lambda: TENANT
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=user_id, role="admin"
    )
    app.include_router(router, prefix="/prefix")
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_state():
    reset_run_store()
    reset_workspace_store()
    yield
    reset_run_store()
    reset_workspace_store()


def _seed(record: RunRecord) -> None:
    get_run_store().save(record)


def test_same_tenant_other_user_cannot_list_or_read_any_run_surface():
    _seed(_record())
    other = _client(runs.router, user_id=OTHER)

    listed = other.get("/prefix")
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 0
    assert listed.json()["items"] == []

    for path in (
        "/prefix/run-inc56",
        "/prefix/run-inc56/events",
        "/prefix/run-inc56/artifact-preview?artifact_id=secret.txt",
        "/prefix/run-inc56/artifacts/secret.txt",
    ):
        response = other.get(path)
        assert response.status_code == 404, (path, response.status_code, response.text)

    stopped = other.post("/prefix/run-inc56/abort")
    assert stopped.status_code == 404, stopped.text
    assert get_run_store().get("run-inc56").status == "running"


def test_owner_still_reads_the_same_surfaces():
    _seed(_record())
    owner = _client(runs.router, user_id=OWNER)

    assert owner.get("/prefix").json()["total"] == 1
    assert owner.get("/prefix/run-inc56").status_code == 200
    preview = owner.get("/prefix/run-inc56/artifact-preview?artifact_id=secret.txt")
    assert preview.status_code == 200
    assert preview.json()["text"] == "private artifact"


@pytest.mark.asyncio
async def test_session_and_parent_references_are_owner_scoped():
    record = _record()
    _seed(record)
    await get_workspace_store().save(WorkspaceRunRecord.from_run_record(record))

    other = _client(workspace.router, user_id=OTHER)
    assert other.get("/prefix/sessions/session-inc56").status_code == 404
    assert other.delete("/prefix/sessions/session-inc56").status_code == 404

    parent_only = other.post(
        "/prefix/tasks",
        json={
            "intent": "continue another user's work",
            "parent_run_id": "run-inc56",
        },
    )
    assert parent_only.status_code == 404, parent_only.text

    owner = _client(workspace.router, user_id=OWNER)
    assert owner.get("/prefix/sessions/session-inc56").status_code == 200


def test_feedback_cannot_be_written_against_another_users_run():
    _seed(_record())
    other = _client(run_outcomes.router, user_id=OTHER)

    response = other.post(
        "/prefix/run-inc56/feedback",
        json={"kind": "like", "idempotency_key": "inc56-other"},
    )
    assert response.status_code == 404, response.text
