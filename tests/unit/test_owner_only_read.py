"""Run surfaces are owner-only — even for privileged roles.

The runs router's owner check and the code-plane loader must refuse any
other user in the same tenant, including admin/manager. This pins the
c3af321 isolation design (see also test_inc56_task_owner_isolation and
the enterprise admission tests): approval is owner confirmation, not
peer review. A manager exception here was tried and reverted — it broke
those isolation tests while tc02's manager client turned out to be the
stale side (its runs are owner-read/owner-approved since).
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from forgeflow.api.routers.codeplane import _load_code_run
from forgeflow.api.routers.runs import _authorize_run_owner
from forgeflow.rbac.models import UserContext
from forgeflow.runtime.orchestrator import RunRecord, get_run_store, reset_run_store

TENANT = "t-priv"
OTHER_TENANT = "t-priv-other"
OWNER = "engineer-1"
RUN_ID = "run-priv-1"


@pytest.fixture(autouse=True)
def _clean_store():
    reset_run_store()
    yield
    reset_run_store()


def _record() -> RunRecord:
    return RunRecord(
        run_id=RUN_ID,
        thread_id=f"{RUN_ID}-th",
        tenant_id=TENANT,
        agent_id=None,
        intent="修改计算逻辑",
        status="awaiting_approval",
        outcome="partial",
        steps=[],
        errors=[],
        created_at="2026-11-03T00:00:00+00:00",
        completed_at=None,
        actor_user_id=OWNER,
        actor_role="admin",
        codeplane={"approval": {"status": "pending"}, "diff": "diff --git a/calc.py"},
    )


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/runs/{RUN_ID}",
            "headers": [],
            "path_params": {"run_id": RUN_ID},
        }
    )


def _user(user_id: str, role: str) -> UserContext:
    return UserContext(user_id=user_id, role=role)


async def test_owner_may_read_own_run():
    get_run_store().save(_record())
    await _authorize_run_owner(_request(), TENANT, _user(OWNER, "viewer"))


@pytest.mark.parametrize("role", ["manager", "admin", "viewer"])
async def test_privileged_non_owner_still_404_on_read(role: str):
    get_run_store().save(_record())
    with pytest.raises(HTTPException) as excinfo:
        await _authorize_run_owner(_request(), TENANT, _user("someone-else", role))
    assert excinfo.value.status_code == 404


async def test_owner_cross_tenant_still_404():
    get_run_store().save(_record())
    with pytest.raises(HTTPException) as excinfo:
        await _authorize_run_owner(_request(), OTHER_TENANT, _user(OWNER, "admin"))
    assert excinfo.value.status_code == 404


async def test_code_run_loader_owner_may_load():
    get_run_store().save(_record())
    record, codeplane = await _load_code_run(RUN_ID, TENANT, OWNER)
    assert record.run_id == RUN_ID
    assert codeplane["approval"]["status"] == "pending"


@pytest.mark.parametrize("role", ["manager", "admin"])
async def test_code_run_loader_privileged_non_owner_404(role: str):
    # _load_code_run takes a bare user_id; the role only matters in that
    # no role exempts a non-owner — hence the parametrize documents the
    # rule even though the loader itself sees just the id.
    _ = role
    get_run_store().save(_record())
    with pytest.raises(HTTPException) as excinfo:
        await _load_code_run(RUN_ID, TENANT, "someone-else")
    assert excinfo.value.status_code == 404
