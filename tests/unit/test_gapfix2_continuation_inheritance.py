"""GAPFIX-INC47 (F-124 Gap C) — continuation resource inheritance.

A Follow-up declares its parent under ``parent_run_id`` (the workspace BFF's
relationship key) or ``continued_from_run_id`` (the ADR-03 injection key). Both
name the same run, so both are recognised, and the parent's **real**
``declared_inputs["resources"]`` are re-dereferenced into the child's planner
inputs — so a continuation that needs the parent's data file is no longer blocked.

The persisted ``declared_inputs`` honestly records the inherited binding plus its
provenance (``inherited_from_parent_run_id``), so the "declared == what the
planner saw" invariant still holds. These tests are additive (red line 1).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from forgeflow.repositories.memory.resource_repo import clear_resource_store
from forgeflow.resources.service import ResourceService, reset_resource_index
from forgeflow.runtime import orchestrator as orch
from forgeflow.runtime.orchestrator import (
    RequestContext,
    TaskCreate,
    _capability_context,
    _declared_inputs,
)

TENANT = "t-gapfix2-c"
CSV = b"region,revenue\nNorth,120000\nSouth,88000\nEast,152000\n"


def _ctx() -> RequestContext:
    return RequestContext(tenant_id=TENANT, user_id="u-gapfix2", role="manager")


@pytest.fixture
def memory_resources(force_memory_backend, tmp_path, monkeypatch):
    from forgeflow.config import get_settings

    monkeypatch.setattr(get_settings(), "resource_store_root", str(tmp_path / "blobs"))
    reset_resource_index()
    clear_resource_store()
    yield
    reset_resource_index()
    clear_resource_store()


def _patch_run_store(monkeypatch, mapping):
    """Make ``get_run_store().get(id)`` return a minimal fake parent."""
    monkeypatch.setattr(
        orch,
        "get_run_store",
        lambda: SimpleNamespace(get=lambda rid: mapping.get(rid)),
    )


async def _register_csv() -> str:
    record = await ResourceService().register_file(TENANT, name="sales.csv", data=CSV)
    return record.id


def _parent(resources):
    return SimpleNamespace(declared_inputs=({"resources": list(resources)} if resources else {}))


# --------------------------------------------------------------------------- #
# Positive — parent resources inherited via ``parent_run_id`` (the BFF key)     #
# --------------------------------------------------------------------------- #
async def test_inherited_via_parent_run_id(
    memory_resources, monkeypatch, tmp_path
):
    csv_id = await _register_csv()
    _patch_run_store(monkeypatch, {"p1": _parent([csv_id])})

    child = TaskCreate(
        intent="继续分析其中增长最快的区域，并说明判断依据",
        workflow_type="generic",
        context={"parent_run_id": "p1"},
    )
    cap = _capability_context(child, _ctx())
    paths = cap.explicit_inputs.get("paths")
    assert paths, "the parent's data file must reach the child's planner inputs"

    declared = _declared_inputs(child)
    assert declared.get("resources") == [csv_id]
    assert declared.get("inherited_from_parent_run_id") == "p1"


async def test_inherited_via_continued_from_run_id(
    memory_resources, monkeypatch
):
    csv_id = await _register_csv()
    _patch_run_store(monkeypatch, {"p1": _parent([csv_id])})

    child = TaskCreate(
        intent="继续：按区域补充",
        workflow_type="generic",
        context={"continued_from_run_id": "p1"},
    )
    cap = _capability_context(child, _ctx())
    assert cap.explicit_inputs.get("paths")


# --------------------------------------------------------------------------- #
# Negatives                                                                     #
# --------------------------------------------------------------------------- #
async def test_parent_without_resources_inherits_nothing(memory_resources, monkeypatch):
    _patch_run_store(monkeypatch, {"p1": _parent([])})
    child = TaskCreate(
        intent="继续：补充文字说明",
        workflow_type="generic",
        context={"parent_run_id": "p1"},
    )
    cap = _capability_context(child, _ctx())
    assert "paths" not in cap.explicit_inputs
    declared = _declared_inputs(child)
    assert "resources" not in declared
    assert "inherited_from_parent_run_id" not in declared


def test_no_parent_no_inheritance(monkeypatch):
    _patch_run_store(monkeypatch, {})
    child = TaskCreate(intent="独立任务", workflow_type="generic", context={})
    cap = _capability_context(child, _ctx())
    assert "paths" not in cap.explicit_inputs
    assert "inherited_from_parent_run_id" not in _declared_inputs(child)


async def test_unknown_parent_degrades_honestly(memory_resources, monkeypatch):
    # A parent not in this process's store (e.g. after a restart) must degrade.
    _patch_run_store(monkeypatch, {})
    child = TaskCreate(
        intent="继续",
        workflow_type="generic",
        context={"parent_run_id": "ghost"},
    )
    cap = _capability_context(child, _ctx())
    assert "paths" not in cap.explicit_inputs
    assert "inherited_from_parent_run_id" not in _declared_inputs(child)


async def test_real_caller_declaration_wins_over_inherited(memory_resources, monkeypatch, tmp_path):
    csv_id = await _register_csv()
    _patch_run_store(monkeypatch, {"p1": _parent([csv_id])})
    explicit_path = str(tmp_path / "explicit.csv")
    child = TaskCreate(
        intent="继续",
        workflow_type="generic",
        context={"parent_run_id": "p1", "paths": [explicit_path]},
    )
    cap = _capability_context(child, _ctx())
    assert cap.explicit_inputs.get("paths") == [explicit_path]
