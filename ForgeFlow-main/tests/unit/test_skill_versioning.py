"""Skill versioning — semver / changelog / diff / rollback (docs §2 ⑤ / P0-03)."""

from __future__ import annotations

import uuid

import pytest

from forgeflow.repositories.memory.skill_repo import MemorySkillRepository
from forgeflow.skills.models import SkillRecord
from forgeflow.skills.versioning import (
    bump_semver,
    create_version,
    diff_specs,
    parse_semver,
    rollback,
)

pytestmark = pytest.mark.asyncio


def _tenant() -> str:
    return f"t-ver-{uuid.uuid4().hex[:8]}"


def test_parse_and_bump_semver():
    assert parse_semver("v1.2.3") == (1, 2, 3)
    assert parse_semver("2") == (2, 0, 0)
    assert bump_semver("0.0.0", "minor") == "0.1.0"
    assert bump_semver("1.2.3", "major") == "2.0.0"
    assert bump_semver("1.2.3", "patch") == "1.2.4"
    assert bump_semver("1.2.3", "nonsense") == "1.2.4"  # falls back to patch


def test_diff_specs_detects_changes():
    old = {"steps": ["a"], "tools": ["x"], "prompt": "p1"}
    new = {"steps": ["a", "b"], "tools": ["x", "y"], "prompt": "p1"}
    diff = diff_specs(old, new)
    assert diff["is_empty"] is False
    assert diff["steps_added"] == ["b"]
    assert diff["tools_added"] == ["y"]

    assert diff_specs(old, dict(old))["is_empty"] is True


async def test_create_version_then_rollback():
    tenant = _tenant()
    repo = MemorySkillRepository()
    skill = SkillRecord(tenant_id=tenant, name="客户流失分析", domain="数据分析")
    await repo.create_skill(skill)

    skill.current_version = "0.0.0"
    v1 = await create_version(
        repo, tenant, skill, {"steps": ["a"], "tools": ["x"]}, bump="minor", actor="u1"
    )
    assert v1.semver == "0.1.0"
    assert skill.current_version == "0.1.0"

    v2 = await create_version(
        repo, tenant, skill, {"steps": ["a", "b"], "tools": ["x", "y"]}, bump="minor", actor="u1"
    )
    assert v2.semver == "0.2.0"

    versions = await repo.list_versions(tenant, skill.id)
    assert {v.semver for v in versions} == {"0.1.0", "0.2.0"}

    restored, diff = await rollback(repo, tenant, skill.id, "0.1.0", actor="u1")
    assert restored.current_version == "0.1.0"
    assert diff["is_empty"] is False


async def test_rollback_unknown_version_raises():
    from forgeflow.skills.errors import GovernanceError

    tenant = _tenant()
    repo = MemorySkillRepository()
    skill = SkillRecord(tenant_id=tenant, name="s", domain="d")
    await repo.create_skill(skill)

    with pytest.raises(GovernanceError):
        await rollback(repo, tenant, skill.id, "9.9.9", actor="u1")
