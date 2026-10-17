from dataclasses import replace

import pytest

from forgeflow.skills.export_bundle import bundle_zip_bytes, write_bundle
from forgeflow.skills.skill_md import SkillBundle


def package():
    return SkillBundle("example-skill", "id", "1.0.0", {"SKILL.md": "instructions"},
                       "hash", {}, {})


@pytest.mark.parametrize("path", ["../escape", "/absolute", "C:/escape", "scripts/../../bad", "a\\b", "a//b"])
def test_export_rejects_unsafe_resource_paths_before_writing(tmp_path, path):
    bundle = replace(package(), files={"SKILL.md": "valid", path: "bad"})
    with pytest.raises(ValueError):
        write_bundle(bundle, tmp_path)
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(ValueError):
        bundle_zip_bytes(bundle)


@pytest.mark.parametrize("slug", ["../escape", "/absolute", "C:\\escape", "", "a/b"])
def test_export_rejects_unsafe_slug(tmp_path, slug):
    with pytest.raises(ValueError):
        write_bundle(replace(package(), slug=slug), tmp_path)
    assert list(tmp_path.iterdir()) == []
