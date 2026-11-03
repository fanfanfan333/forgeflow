"""Keyword passthrough in _resolve_injected_skills (keyword skills, Phase 1).

A version spec may declare author-controlled ``keywords``; they travel
with the injected entry so delegation's _brief_skills can match on them.
A spec without keywords keeps the exact old entry shape.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from forgeflow.runtime import orchestrator as orch

pytestmark = pytest.mark.asyncio


class _FakeRegistry:
    def __init__(self, specs):
        self._specs = specs

    async def get(self, tenant_id, skill_id):
        return SimpleNamespace(
            id=skill_id, name=f"skill-{skill_id}", description="d", current_version="1.0"
        )

    async def versions(self, tenant_id, skill_id):
        return [SimpleNamespace(semver="1.0", spec=self._specs.get(skill_id, {}))]


async def test_resolve_carries_declared_keywords(monkeypatch):
    monkeypatch.setattr(
        "forgeflow.skills.registry.SkillRegistry",
        lambda: _FakeRegistry(
            {
                "sk1": {"steps": ["s1"], "keywords": ["客户流失", ""]},
                "sk2": {"steps": ["s1"]},
            }
        ),
    )
    resolved = await orch._resolve_injected_skills("t-1", ["sk1", "sk2"])
    by_id = {e["id"]: e for e in resolved}
    assert by_id["sk1"]["keywords"] == ["客户流失"]  # empty entries cleaned
    assert "keywords" not in by_id["sk2"]  # absent stays absent
