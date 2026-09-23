"""G1 — a terminal run produces an Experience (jump ②, docs §4 / P0-01)."""

from __future__ import annotations

import uuid

import pytest

from forgeflow.experience.extractor import ExperienceExtractor
from forgeflow.repositories.memory.experience_repo import MemoryExperienceRepository

pytestmark = pytest.mark.asyncio


def _tenant() -> str:
    return f"t-exp-{uuid.uuid4().hex[:8]}"


async def test_success_run_produces_experience():
    tenant = _tenant()
    repo = MemoryExperienceRepository()
    run = {
        "run_id": "r-success-1",
        "tenant_id": tenant,
        "intent": "分析华东地区销售数据并生成报告",
        "status": "completed",
        "tags": ["数据分析"],
        "steps": [{"tool": "data.query"}, {"tool": "report.render"}],
    }

    exp = await ExperienceExtractor(repo=repo).extract(run, None, tenant_id=tenant)

    assert exp.outcome == "success"
    assert exp.run_id == "r-success-1"
    assert exp.tenant_id == tenant
    assert exp.summary
    # reusable steps distil the tool trace
    assert [s["tool"] for s in exp.reusable_steps] == ["data.query", "report.render"]
    # embedded so similarity search works offline
    assert exp.embedding and len(exp.embedding) > 0


async def test_failure_run_marks_failure_outcome():
    tenant = _tenant()
    repo = MemoryExperienceRepository()
    run = {"run_id": "r-fail-1", "tenant_id": tenant, "intent": "失败任务", "status": "failed",
           "errors": ["工具异常"], "steps": [{"tool": "data.query"}]}

    exp = await ExperienceExtractor(repo=repo).extract(run, None, tenant_id=tenant)

    assert exp.outcome == "failure"
    assert any(d.get("source") == "error" for d in exp.decisions)


async def test_experience_links_source_memories():
    tenant = _tenant()
    repo = MemoryExperienceRepository()
    run = {"run_id": "r-link-1", "tenant_id": tenant, "intent": "带记忆的任务", "status": "completed",
           "steps": [{"tool": "x"}]}

    exp = await ExperienceExtractor(repo=repo).extract(
        run, None, memories=[{"id": "mem-1"}, {"id": "mem-2"}], tenant_id=tenant
    )

    memories = await repo.list_memories(tenant, exp.id)
    assert set(memories) == {"mem-1", "mem-2"}
    assert set(exp.memory_ids) == {"mem-1", "mem-2"}


async def test_extract_terminal_skips_non_terminal():
    tenant = _tenant()
    repo = MemoryExperienceRepository()
    run = {"run_id": "r-running", "tenant_id": tenant, "intent": "进行中", "status": "running"}

    result = await ExperienceExtractor(repo=repo).extract_terminal(run, None, tenant_id=tenant)

    assert result is None
    assert await repo.count(tenant) == 0
