"""FF-R1 P5 — guards around the persisted run detail (BIZ-14).

The brief's two hard requirements on top of the snapshot port:

* **Tenant isolation**: a run's detail is part of its workspace
  record — reading it under another tenant must return nothing,
  exactly like the header itself.
* **No internal-field leakage**: the projection is served back in
  API responses, so ``_``-prefixed platform keys (reserved context,
  streak state, injected bookkeeping) must never appear in it, at
  any depth — invocation arguments and free-form sections included.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from forgeflow.workspace.detail import build_detail
from forgeflow.workspace.models import WorkspaceRunRecord
from forgeflow.workspace.store import MemoryWorkspaceStore

pytestmark = pytest.mark.asyncio


def _all_keys(node) -> list[str]:
    keys: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            keys.append(str(key))
            keys.extend(_all_keys(value))
    elif isinstance(node, list):
        for item in node:
            keys.extend(_all_keys(item))
    return keys


def test_projection_never_contains_underscore_prefixed_keys():
    record = SimpleNamespace(
        steps=[
            {"step": 1, "tool": "docs.parse", "status": "ok", "_identical_call_state": {"x": 1}}
        ],
        tool_invocations=[
            {
                "tool": "docs.parse",
                "status": "ok",
                "arguments": {"path": "a.md", "_recovery_state": {"secret": 1}},
            }
        ],
        observations=[],
        errors=[],
        plan={"source": "react", "_checkpoint_resume": {"seed": 1}, "steps": []},
        loop={"_hit_max_iterations": True, "rounds": 2},
        persistence={},
        plan_source="react",
        skills_used=[],
        llm={"execution_budget": {"model_calls": 3, "_internal_cap": 9}},
    )
    detail = build_detail(record)
    leaked = [key for key in _all_keys(detail) if key.startswith("_")]
    assert leaked == []
    # The legitimate content survived the stripping.
    assert detail["steps"][0]["tool"] == "docs.parse"
    assert detail["tool_invocations"][0]["arguments"] == {"path": "a.md"}
    assert detail["plan"]["source"] == "react"
    assert detail["budget"] == {"model_calls": 3}


async def test_detail_read_is_tenant_isolated():
    store = MemoryWorkspaceStore()
    record = WorkspaceRunRecord(
        run_id="run-1",
        tenant_id="t1",
        session_id="run-1",
        actor_user_id="u1",
        intent="quarterly report",
        detail={"version": 1, "steps": [{"tool": "docs.parse"}], "truncated": False},
    )
    await store.save(record)
    assert await store.get("t2", "run-1") is None
    own = await store.get("t1", "run-1")
    assert own is not None
    assert own.detail["steps"] == [{"tool": "docs.parse"}]
