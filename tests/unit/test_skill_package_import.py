import io
import zipfile

import pytest

from forgeflow.skills.export_bundle import bundle_zip_bytes
from forgeflow.skills.models import SkillRecord, SkillVersionRecord
from forgeflow.skills.package_import import MAX_FILE_BYTES, package_spec, read_package
from forgeflow.skills.progressive_loader import ProgressiveLoader
from forgeflow.skills.skill_md import SkillMaterializationError, materialize_skill

SOURCE = "---\nname: example-skill\ndescription: Read and clean documents when requested.\nallowed-tools: docs.parse\n---\n\nRead before editing. Preserve numbers.\n"


def test_omitted_tool_restriction_inherits_scope_but_explicit_empty_denies_all():
    assert "tools" not in package_spec({"SKILL.md": SOURCE.replace("allowed-tools: docs.parse\n", "")})
    assert package_spec({"SKILL.md": SOURCE.replace("docs.parse", "''")})["tools"] == []
    assert package_spec({"SKILL.md": SOURCE})["tools"] == ["docs.parse"]


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as stream:
        for key, value in files.items():
            info = zipfile.ZipInfo()
            info.filename = key
            stream.writestr(info, value)
    return output.getvalue()


def test_import_export_and_explicit_resource_read_preserve_original_content():
    files = {"SKILL.md": SOURCE, "references/check.md": "Preserve formulae.",
             "scripts/check.py": "raise RuntimeError('never execute on import')",
             "assets/evals/holdout.json": '{"case": "independent"}'}
    spec = read_package(archive(files), zip_format=True)
    assert spec["import_provenance"] == {"trusted": False, "scripts_executed": False}
    skill = SkillRecord(id="skill", name="example-skill", current_version="1.0.0")
    version = SkillVersionRecord(skill_id="skill", semver="1.0.0", spec=spec)
    bundle = materialize_skill(skill, version)
    assert bundle.files == files
    again = read_package(bundle_zip_bytes(bundle), zip_format=True)
    assert again["package_hash"] == spec["package_hash"]
    loader = ProgressiveLoader(skills=[skill], versions={"skill": version})
    assert loader.load_resource("skill", "references/check.md") == files["references/check.md"]
    assert loader.load_resource("skill", "scripts/check.py") == files["scripts/check.py"]


@pytest.mark.parametrize("path", ["../escape", "scripts/../../escape", "C:/escape", "scripts\\escape", "/escape"])
def test_import_rejects_unsafe_archive_paths(path):
    with pytest.raises(ValueError):
        read_package(archive({"SKILL.md": SOURCE, path: "danger"}), zip_format=True)


def test_import_rejects_zip_bomb_duplicate_binary_and_invalid_markdown():
    with pytest.raises(ValueError, match="oversized"):
        read_package(archive({"SKILL.md": SOURCE, "assets/huge.txt": "a" * (MAX_FILE_BYTES + 1)}), zip_format=True)
    with pytest.warns(UserWarning), pytest.raises(ValueError, match="Duplicate"):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as stream:
            stream.writestr("SKILL.md", SOURCE)
            stream.writestr("SKILL.md", SOURCE)
        read_package(output.getvalue(), zip_format=True)
    with pytest.raises(UnicodeError):
        read_package(archive({"SKILL.md": SOURCE, "assets/binary": b"\xff"}), zip_format=True)
    with pytest.raises(ValueError, match="Invalid SKILL"):
        read_package(b"no metadata", zip_format=False)


def test_changed_persisted_package_hash_is_not_materialized():
    spec = package_spec({"SKILL.md": SOURCE})
    spec["package_files"]["SKILL.md"] += " changed"
    with pytest.raises(SkillMaterializationError, match="hash mismatch"):
        materialize_skill(SkillRecord(id="skill"), SkillVersionRecord(spec=spec))


def test_real_import_route_keeps_draft_and_isolates_tenants(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from forgeflow.api.routers import skills
    from forgeflow.auth.jwt import create_access_token
    from forgeflow.middleware.auth import RBACMiddleware
    from forgeflow.repositories.memory.skill_repo import MemorySkillRepository
    from tests.business_auth import authenticate_business_app

    repo = MemorySkillRepository()
    monkeypatch.setattr(skills, "get_skill_repository", lambda: repo)
    app = FastAPI()
    authenticate_business_app(app)
    app.add_middleware(RBACMiddleware)
    app.include_router(skills.router, prefix="/skills")
    client = TestClient(app)

    def headers(role="admin", tenant="package-import-test"):
        return {"Authorization": "Bearer " + create_access_token(
            user_id="package-import-user", role=role, workspace_id=tenant),
            "content-type": "text/markdown"}

    assert client.post("/skills/import", content=SOURCE, headers=headers("viewer")).status_code == 403
    response = client.post("/skills/import", content=SOURCE, headers=headers())
    assert response.status_code == 201, response.text
    skill = response.json()
    assert skill["status"] == "draft" and skill["current_version"] == "1.0.0"
    assert client.post("/skills/import", content=SOURCE, headers=headers()).status_code == 409
    # Registry instances used by read routes must see the same tenant-scoped repo.
    monkeypatch.setattr("forgeflow.skills.registry.get_skill_repository", lambda: repo)
    target = "/skills/" + skill["id"]
    detail = client.get(target + "/instructions", headers=headers()).json()
    assert detail["source"] == SOURCE and detail["kind"] == "skill_package"
    exported = client.get(target + "/export?format=zip", headers=headers())
    assert exported.status_code == 200
    assert read_package(exported.content, zip_format=True)["package_files"]["SKILL.md"] == SOURCE
    assert client.get(target + "/export?format=zip", headers=headers(tenant="other-package-tenant")).status_code == 404
    assert client.post(target + "/instructions", headers={**headers(), "content-type": "application/json"}, json={
        "name": "example-skill", "description": "changed", "instructions": "changed instructions safely"
    }).status_code == 409
