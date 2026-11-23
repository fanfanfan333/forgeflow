import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_same_tool_name_with_new_schema_rebuilds_graphs():
    from forgeflow.graph.refresh import rebuild_graphs_if_changed
    old = SimpleNamespace(name="lookup", description="read", args_schema={"required": ["name"]})
    new = SimpleNamespace(name="lookup", description="read", args_schema={"required": ["id"]})
    state = SimpleNamespace(mcp_tools=[old], graphs={}, graph=None)
    compile_graph = AsyncMock(return_value=object())
    assert await rebuild_graphs_if_changed(state, [new], compile_graph)
    assert compile_graph.await_count == 3


def test_dead_claim_owner_is_released(tmp_path, monkeypatch):
    import forgeflow.runtime.recovery as recovery
    store = recovery.RecoveryStore(tmp_path / "journal.sqlite3")
    version = store.save("t", "u", "r", {"messages": [{"content": "continue"}]}, status="paused")
    store.claim("t", "u", "r", version)
    monkeypatch.setattr(recovery, "_lock_owner", lambda *args, **kwargs: True)
    snapshot = store.get("t", "u", "r")
    assert snapshot["status"] == "paused"
    assert snapshot["version"] > version


@pytest.mark.asyncio
async def test_write_response_loss_is_unknown(monkeypatch):
    from forgeflow.mcp.client import sessions
    client = SimpleNamespace(call_tool=AsyncMock(side_effect=TimeoutError("response lost")))
    class Connection:
        async def __aenter__(self):
            return client
        async def __aexit__(self, *args):
            pass
    monkeypatch.setattr(sessions, "make_client", lambda *args: Connection())
    monkeypatch.setattr(sessions, "issue_token", lambda *args: "test")
    ticket = SimpleNamespace(to_claim=lambda: {})
    with pytest.raises(sessions.OutcomeUnknown):
        await sessions._call_once("target", "t", "u", "admin", "write", {}, ticket)
    client.call_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_stream_obeys_workflow_timeout(monkeypatch):
    from forgeflow.api.routers import workflows
    from forgeflow.config import get_settings
    class Pipeline:
        async def stream(self, *args, **kwargs):
            await asyncio.sleep(1)
            yield {"event": "workflow_complete"}
    monkeypatch.setattr(workflows, "_dispatch_pipeline", lambda *args: (Pipeline(), {}))
    monkeypatch.setattr(get_settings(), "workflow_run_timeout_seconds", 0.01)
    response = await workflows.stream_workflow(
        SimpleNamespace(workflow_type="sales_ops", lead_data={}, dry_run=True),
        graphs={"sales_ops": object()}, pool=None,
        user=SimpleNamespace(user_id="u", role="admin"), workspace_id=None,
    )
    chunks = [chunk async for chunk in response.body_iterator]
    assert any('"event": "error"' in chunk for chunk in chunks)
    assert not any('"event": "workflow_complete"' in chunk for chunk in chunks)


def test_live_claim_is_not_released(tmp_path):
    from forgeflow.runtime.recovery import RecoveryStore
    store = RecoveryStore(tmp_path / "live.sqlite3")
    version = store.save("t", "u", "r", {"messages": [{"content": "continue"}]}, status="paused")
    store.claim("t", "u", "r", version)
    assert store.get("t", "u", "r")["status"] == "claimed"


def test_claim_records_the_current_owner(tmp_path, monkeypatch):
    import forgeflow.runtime.recovery as recovery
    store = recovery.RecoveryStore(tmp_path / "owner.sqlite3")
    version = store.save("t", "u", "r", {"messages": [{"content": "continue"}]}, status="paused")
    monkeypatch.setattr(recovery, "_OWNER", "a" * 32)
    store.claim("t", "u", "r", version)
    with store.connect() as connection:
        import json
        body = connection.execute("SELECT body FROM checkpoints").fetchone()[0]
    assert json.loads(body)["owner"] == "a" * 32


@pytest.mark.asyncio
async def test_write_connection_close_failure_is_unknown(monkeypatch):
    from forgeflow.mcp.client import sessions
    class Connection:
        async def __aenter__(self):
            return SimpleNamespace(call_tool=AsyncMock(return_value={"ok": True}))
        async def __aexit__(self, *args):
            raise ConnectionError("close failed")
    monkeypatch.setattr(sessions, "make_client", lambda *args: Connection())
    monkeypatch.setattr(sessions, "issue_token", lambda *args: "test")
    with pytest.raises(sessions.OutcomeUnknown):
        await sessions._call_once("target", "t", "u", "admin", "write", {}, SimpleNamespace(to_claim=lambda: {}))
