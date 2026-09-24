"""REST contract for the Agent Loop budget-breaker audit trail (INC5).

The breaker's verdict was already emitted on SSE (``run.loop.breaker``) and
returned on ``RunHandle.detail["loop"]``, but the *persisted* ``RunRecord`` —
which is what ``GET /runs/{id}`` actually reads — never carried it. So the audit
evidence was unreachable from REST: the classic "looks wired, silently isn't"
gap this project keeps hunting.

These tests pin the full four-layer chain end to end::

    RunRecord.loop  ->  run_task construction
                    ->  RunDetailResponse.loop  ->  GET /runs/{id}

Both directions matter:

* a run that blows its budget must surface the breach through REST, and
* a run under the shipped defaults must stay untripped (no behaviour change) —
  the endpoint must never fabricate a trip.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.routers import runs as runs_router
from forgeflow.config import get_settings
from forgeflow.runtime.orchestrator import (
    RequestContext,
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


def _failing_task(**context: object) -> TaskCreate:
    """A task the executor reports as failing, so the replan loop must run."""
    return TaskCreate(
        intent="分析华东销售数据",
        context={"simulate_failure": True, **context},
    )


def _runs_client(tenant: str, record) -> TestClient:
    """Mount the real runs router with the tenant pinned and ``record`` stored.

    Mirrors ``test_runtime_llm_agent._runs_client`` — same dependency override
    and same router include — so this file exercises the exact production route
    rather than a bespoke handler.
    """
    app = FastAPI()
    app.dependency_overrides[resolve_tenant] = lambda: tenant
    get_run_store().save(record)
    app.include_router(runs_router.router, prefix="/runs")
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_state():
    """Every test starts from an empty run store."""
    reset_run_store()
    yield
    reset_run_store()


@pytest.mark.asyncio
async def test_run_detail_exposes_a_tripped_loop_breaker(monkeypatch):
    """A run that blows its token budget must prove it through REST, not only on
    the transient SSE frame."""
    # A tiny ceiling plus a usage entry that already exceeds it ⇒ the first
    # authorised retry is vetoed and the run escalates on observation #1.
    monkeypatch.setattr(get_settings(), "max_run_tokens", 1000)
    task = _failing_task(
        llm_usage=[{"agent": "supervisor", "input_tokens": 800, "output_tokens": 400}]
    )
    ctx = RequestContext(tenant_id="t-runs-loop-trip", user_id="admin-1", role="admin")

    handle = await run_task(task, ctx, bus=_RecordingBus())
    record = get_run_store().get(handle.run_id)
    assert record is not None

    client = _runs_client(record.tenant_id, record)
    response = client.get(f"/runs/{handle.run_id}")
    assert response.status_code == 200, response.text
    loop = response.json()["loop"]

    assert loop["tripped"] is True
    assert loop["breaches"][0]["dimension"] == "tokens"
    assert loop["observations"] >= 1


@pytest.mark.asyncio
async def test_run_detail_keeps_a_clean_loop_breaker_untripped():
    """Negative control: under the shipped defaults a failing run still replans
    twice and the breaker never trips — the REST view must not fabricate one."""
    ctx = RequestContext(tenant_id="t-runs-loop-default", user_id="admin-1", role="admin")

    handle = await run_task(_failing_task(), ctx, bus=_RecordingBus())
    record = get_run_store().get(handle.run_id)
    assert record is not None

    client = _runs_client(record.tenant_id, record)
    response = client.get(f"/runs/{handle.run_id}")
    assert response.status_code == 200, response.text
    loop = response.json()["loop"]

    assert loop["tripped"] is False
    assert loop["observations"] == 2
    assert loop["breaches"] == []
