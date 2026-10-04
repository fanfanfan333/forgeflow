"""Gap-fix regression: ``GET /metrics/`` must surface ``source_detail`` (F-135).

Background (R5 closure, ``forgeflow-r5-closure-2026-10-02.md`` §四 D5)
--------------------------------------------------------------------
``forgeflow/observability/metrics_source.py`` already returns the honest
provenance of a KPI — ``source_detail`` ∈ {``hub_runs``, ``workspace_runs``,
``workflow_runs``} — and the realstack T3 case asserts it **at the source
layer**. But ``GET /metrics/`` wraps the payload in
:class:`~forgeflow.api.schemas.MetricsSummaryResponse`, which did **not**
declare ``source_detail`` ⇒ pydantic v2 (default ``extra="ignore"``) silently
dropped it. The taskbook requires the *response* to carry
``source_detail == "workspace_runs"`` on the hub path; the source-layer PASS
therefore did **not** imply an API-layer PASS.

These cases pin the fix at the boundary the defect lived on: the response
model, and the router that builds it. They are deliberately **API-surface**
tests (not source-layer tests), so a future regression that re-narrows the
response model turns them red.
"""

from __future__ import annotations

import pytest

from forgeflow.api.routers.metrics import get_metrics_summary
from forgeflow.api.schemas import MetricsSummaryResponse
from forgeflow.config import get_settings

_TENANT = get_settings().default_tenant_id


def test_response_model_declares_source_detail() -> None:
    """The field must be declared — otherwise pydantic drops it silently."""
    assert "source_detail" in MetricsSummaryResponse.model_fields


def test_source_detail_survives_model_construction() -> None:
    """The exact F-135 counterfactual: an input ``source_detail`` must survive.

    Feed the hub-path provenance and assert it comes back out of
    ``model_dump()`` — the precise behaviour that was broken (input
    ``"workspace_runs"`` → previously ``<<dropped>>``).
    """
    payload = {
        "total_runs": 3,
        "terminal_runs": 2,
        "success_rate": 0.5,
        "has_data": True,
        "source": "postgres",
        "source_detail": "workspace_runs",
    }
    dumped = MetricsSummaryResponse(**payload).model_dump()
    assert dumped["source_detail"] == "workspace_runs"
    # ``source`` keeps its historical (backward-compatible) value.
    assert dumped["source"] == "postgres"


def test_source_detail_defaults_to_not_reported_not_a_fabricated_table() -> None:
    """A caller that reports no provenance gets ``""`` — never an invented table."""
    dumped = MetricsSummaryResponse().model_dump()
    assert dumped["source_detail"] == ""


@pytest.fixture(autouse=True)
def _memory_backend(force_memory_backend):
    """Pin the memory backend — the source then reports ``source_detail="hub_runs"``."""
    return force_memory_backend


@pytest.mark.asyncio
async def test_router_response_carries_the_source_layer_provenance() -> None:
    """End-to-end at the router: the source's ``source_detail`` reaches the response.

    Positive control: the source layer definitely produces ``hub_runs`` in the
    memory profile, so the router response must expose the **same** value —
    proving the model no longer drops it.
    """
    from forgeflow.observability.metrics_source import MemoryMetricsSource

    source_value = (await MemoryMetricsSource().summary(_TENANT))["source_detail"]
    assert source_value == "hub_runs"  # positive control on the source layer

    response = await get_metrics_summary(tenant=_TENANT)
    assert response.model_dump()["source_detail"] == source_value
