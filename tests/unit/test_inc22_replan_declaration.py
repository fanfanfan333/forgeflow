"""INC22 W1 — a manual replan re-declares the **original task's** inputs.

Why this file exists
--------------------
``POST /runs/{id}/replan`` (:func:`forgeflow.api.routers.runs.replan_run`) used to
rebuild the task from ``intent`` alone::

    task = TaskCreate(intent=record.intent, title=record.intent[:60])

so the re-run lost ``workflow_type`` (falling back to ``"generic"``) and **every**
explicit caller input (``table`` / ``paths`` / ``repo_path`` …). A task that had
just executed was therefore replayed as a bare intent and came back ``blocked`` —
the user-visible "Agent 做了一半" symptom. The root cause was structural: the
persisted :class:`~forgeflow.runtime.orchestrator.RunRecord` never carried the
declaration, so replan could not restore it.

INC22 W1 makes the declaration first-class and pins the whole chain end to end::

    RunRecord.workflow_type / declared_inputs
        ->  run_task construction
        ->  RunDetailResponse.{workflow_type, declared_inputs}  ->  GET /runs/{id}
        ->  replan_run rebuilds TaskCreate from the record
        ->  the NEW run carries the SAME declaration (byte-for-byte)

Both directions matter:

* a run that **did** declare a domain + inputs must survive a replan verbatim, and
* a run that declared **nothing** must stay empty — the platform never infers or
  default-fills a ``table`` (the counterfactual below is the discriminating half).

The route is exercised through the **real** runs router (same dependency-override
pattern as ``test_runs_api.py`` / ``test_inc12_trust_loop.py``), against the
offline ``deterministic`` runtime, so the whole declaration chain is measured on
production code rather than a bespoke handler.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.dependencies import get_current_user
from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.routers import runs as runs_router
from forgeflow.rbac.models import UserContext
from forgeflow.runtime.orchestrator import (
    RequestContext,
    RunRecord,
    TaskCreate,
    get_run_store,
    reset_run_store,
    run_task,
)


class _RecordingBus:
    """Minimal bus stub — ``run_task`` only ever calls ``emit``."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, run_id: str, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


TENANT = "t-inc22-replan"
REPLICATOR = UserContext(user_id="u-inc22-replicator", role="admin")


def _runs_client() -> TestClient:
    """Mount the real runs router with the tenant + user pinned.

    Mirrors ``test_inc12_trust_loop.test_q6_replan_recovery_is_attributed`` — same
    two dependency overrides and same router include — so the replan path under
    test is the exact production route.
    """
    app = FastAPI()
    app.dependency_overrides[resolve_tenant] = lambda: TENANT
    app.dependency_overrides[get_current_user] = lambda: REPLICATOR
    app.include_router(runs_router.router, prefix="/runs")
    return TestClient(app)


def _ctx() -> RequestContext:
    return RequestContext(tenant_id=TENANT, user_id="u-inc22-author", role="admin")


@pytest.fixture(autouse=True)
def _clean_state():
    """Every test starts from an empty run store."""
    reset_run_store()
    yield
    reset_run_store()


@pytest.mark.asyncio
async def test_declared_run_survives_a_replan_verbatim(force_memory_backend):
    """A run that declared ``sales_ops`` + ``table`` keeps **both** after replan."""
    handle = await run_task(
        TaskCreate(
            intent="整理本周华南区销售数据并生成周度经营报告",
            workflow_type="sales_ops",
            context={"table": "sales_weekly"},
        ),
        _ctx(),
        bus=_RecordingBus(),
    )

    client = _runs_client()

    # The original run's declaration is readable through REST.
    original = client.get(f"/runs/{handle.run_id}")
    assert original.status_code == 200, original.text
    body = original.json()
    assert body["workflow_type"] == "sales_ops"
    assert body["declared_inputs"] == {"table": "sales_weekly"}

    # Replan re-runs the intent as a NEW run.
    replan = client.post(
        f"/runs/{handle.run_id}/replan", json={"reason": "INC22 W1 verify"}
    )
    assert replan.status_code == 200, replan.text
    new_run_id = replan.json()["run_id"]
    assert new_run_id != handle.run_id

    # The new run carries the SAME declaration, byte-for-byte.
    replay = client.get(f"/runs/{new_run_id}")
    assert replay.status_code == 200, replay.text
    replayed = replay.json()
    assert replayed["workflow_type"] == body["workflow_type"]
    assert replayed["declared_inputs"] == body["declared_inputs"]


@pytest.mark.asyncio
async def test_undeclared_run_stays_empty_after_a_replan(force_memory_backend):
    """Counterfactual (discriminating): no declaration ⇒ no invented ``table``.

    A run created without any explicit input must report ``declared_inputs == {}``
    and ``workflow_type == "generic"`` — and its replan must stay equally empty.
    If W1 ever default-filled or inferred a ``table`` this assertion fails.
    """
    handle = await run_task(
        TaskCreate(intent="离线整理一份摘要（未声明任何输入）"),
        _ctx(),
        bus=_RecordingBus(),
    )

    client = _runs_client()

    original = client.get(f"/runs/{handle.run_id}")
    assert original.status_code == 200, original.text
    body = original.json()
    assert body["workflow_type"] == "generic"
    assert body["declared_inputs"] == {}
    assert "table" not in body["declared_inputs"]

    replan = client.post(
        f"/runs/{handle.run_id}/replan", json={"reason": "INC22 counterfactual"}
    )
    assert replan.status_code == 200, replan.text
    new_run_id = replan.json()["run_id"]

    replayed = client.get(f"/runs/{new_run_id}")
    assert replayed.status_code == 200, replayed.text
    replay_body = replayed.json()
    assert replay_body["declared_inputs"] == {}
    assert "table" not in replay_body["declared_inputs"]
    assert replay_body["workflow_type"] == "generic"


def test_run_record_defaults_are_safe():
    """``RunRecord`` still constructs with the pre-INC22 positional/kw fields.

    The two INC22 fields are additive + default-safe: an old construction site
    that passes neither must build a valid record whose declaration honestly
    degrades to the producer's own defaults (``"generic"`` / ``{}``).
    """
    record = RunRecord(
        run_id="r-1",
        thread_id="t-1",
        tenant_id=None,
        agent_id=None,
        intent="x",
        status="completed",
        outcome="success",
        steps=[],
        errors=[],
        created_at="t",
    )
    assert record.workflow_type == "generic"
    assert record.declared_inputs == {}


def test_run_detail_degrades_honestly_for_a_pre_inc22_record():
    """A stored record without the INC22 attributes must not break ``GET``.

    ``get_run`` reads them through ``getattr`` (the repo's existing
    honest-degradation style); deleting the attributes simulates a record written
    by a pre-INC22 process. The endpoint must still answer 200 with the producer's
    own defaults instead of raising ``AttributeError``.
    """
    record = RunRecord(
        run_id="r-pre22",
        thread_id="t-pre22",
        tenant_id=TENANT,
        agent_id=None,
        intent="legacy",
        status="completed",
        outcome="success",
        steps=[],
        errors=[],
        created_at="t",
    )
    # Simulate a pre-INC22 record: the attributes simply do not exist.
    del record.workflow_type
    del record.declared_inputs
    get_run_store().save(record)

    client = _runs_client()
    response = client.get("/runs/r-pre22")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["workflow_type"] == "generic"
    assert body["declared_inputs"] == {}
