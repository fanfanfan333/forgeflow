import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from forgeflow.api.routers import workflows
from forgeflow.api.schemas import WorkflowRunRequest


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", [1, 2])
async def test_workflow_never_reports_success_when_required_record_is_missing(
    monkeypatch, failure_at
):
    run_id, thread_id, token = (str(uuid.uuid4()) for _ in range(3))
    pipeline = SimpleNamespace(
        run=AsyncMock(
            return_value=(run_id, thread_id, {"current_stage": "approve", "approval_token": token})
        )
    )
    monkeypatch.setattr(workflows, "_dispatch_pipeline", lambda *args: (pipeline, object()))
    monkeypatch.setattr(workflows, "notify_approval_request", AsyncMock())
    monkeypatch.setattr(
        "forgeflow.observability.metrics_store.MetricsStore",
        lambda pool: SimpleNamespace(record_run_completion=AsyncMock()),
    )
    conn = MagicMock()
    conn.execute = AsyncMock(
        side_effect=[RuntimeError("database offline")]
        if failure_at == 1
        else [None, RuntimeError("database offline")]
    )
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    user = SimpleNamespace(user_id="test-user", role="admin")
    with pytest.raises(HTTPException) as error:
        await workflows.run_workflow(
            WorkflowRunRequest(workflow_type="sales_ops", lead_data={}, dry_run=True),
            graphs={"sales_ops": object()},
            pool=pool,
            user=user,
            workspace_id=None,
        )
    assert error.value.status_code == 503
    assert run_id in error.value.detail
    workflows.notify_approval_request.assert_not_awaited()
