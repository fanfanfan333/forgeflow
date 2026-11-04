"""Phase 9 B8 — multi-skill pack loading (validation layer, no store writes)."""

from __future__ import annotations

import json

import pytest

from forgeflow.skills.pack import load_skill_pack


def _skill_md(name: str, description: str = "A test skill") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\nDo the thing.\n"


def _pack(skills: dict[str, str], extra: dict[str, str] | None = None) -> dict[str, str]:
    files = {
        "pack.json": json.dumps({"name": "test-pack", "version": "1.0.0", "skills": sorted(skills)}),
    }
    for dirname, slug in skills.items():
        files[f"skills/{dirname}/SKILL.md"] = _skill_md(slug)
    files.update(extra or {})
    return files


def test_valid_pack_loads_all_members() -> None:
    pack = load_skill_pack(_pack({"alpha": "alpha-skill", "beta": "beta-skill"}))
    assert pack["kind"] == "skill_pack"
    assert pack["name"] == "test-pack"
    assert [s["manifest"]["name"] for s in pack["skills"]] == ["alpha-skill", "beta-skill"]
    assert len(pack["pack_hash"]) == 64


def test_pack_requires_manifest_and_members() -> None:
    with pytest.raises(ValueError, match="pack.json"):
        load_skill_pack({"skills/a/SKILL.md": _skill_md("a")})
    with pytest.raises(ValueError, match="no SKILL.md"):
        load_skill_pack({
            "pack.json": json.dumps({"name": "p", "version": "1", "skills": ["alpha"]}),
            "skills/alpha/references/note.md": "x",
        })
    with pytest.raises(ValueError, match="not valid JSON"):
        load_skill_pack({"pack.json": "{nope", "skills/a/SKILL.md": _skill_md("a")})


def test_pack_rejects_stray_files_and_duplicate_slugs() -> None:
    with pytest.raises(ValueError, match="unexpected file"):
        load_skill_pack(_pack({"alpha": "alpha-skill"}, extra={"random.txt": "x"}))
    with pytest.raises(ValueError, match="duplicate skill slug"):
        load_skill_pack(_pack({"alpha": "same", "beta": "same"}))
    with pytest.raises(ValueError, match="1-16"):
        load_skill_pack({"pack.json": json.dumps({"name": "p", "version": "1", "skills": []})})
