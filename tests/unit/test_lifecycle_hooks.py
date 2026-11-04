"""Phase 9 B11 — agent lifecycle hooks (observe-only, never breaking a run)."""

from __future__ import annotations

import pytest

from forgeflow.runtime.lifecycle_hooks import (
    clear_lifecycle_hooks,
    register_lifecycle_hook,
    run_lifecycle_hooks,
)


@pytest.fixture(autouse=True)
def _clean():
    clear_lifecycle_hooks()
    yield
    clear_lifecycle_hooks()


def test_unknown_event_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown lifecycle event"):
        register_lifecycle_hook("explode", lambda e, p: None)


@pytest.mark.asyncio
async def test_hooks_run_in_order_and_a_raising_hook_does_not_break() -> None:
    seen: list = []

    def _boom(event, payload):
        raise RuntimeError("buggy hook")

    async def _recorder(event, payload):
        seen.append((event, payload["run_id"]))

    register_lifecycle_hook("run_finished", _boom)
    register_lifecycle_hook("run_finished", _recorder)
    await run_lifecycle_hooks("run_finished", {"run_id": "r-1"})
    assert seen == [("run_finished", "r-1")]


@pytest.mark.asyncio
async def test_run_task_fires_started_finished_and_attempt(monkeypatch) -> None:
    """End-to-end through the react test harness: one run, three events."""
    import tests.unit.test_react_wiring as wiring
    from forgeflow.runtime.lifecycle_hooks import register_lifecycle_hook
    from forgeflow.runtime.orchestrator import RequestContext, TaskCreate, run_task

    seen: list = []

    def _recorder(event, payload):
        seen.append(event)

    for event in ("run_started", "run_finished", "attempt_finished"):
        register_lifecycle_hook(event, _recorder)

    fake = wiring._FakeReactModel([])
    wiring._patch_react(monkeypatch, fake)
    task = TaskCreate(intent="请查看 /nope1.txt 的内容", workflow_type="generic")
    ctx = RequestContext(tenant_id="t-react", user_id="u-1", role="admin")
    await run_task(task, ctx, bus=wiring._RecordingBus())
    assert seen.count("run_started") == 1
    assert seen.count("run_finished") == 1
    assert seen.count("attempt_finished") >= 1
