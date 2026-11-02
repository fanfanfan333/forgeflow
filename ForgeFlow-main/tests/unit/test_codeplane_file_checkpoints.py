import hashlib
from pathlib import Path

import pytest

from forgeflow.codeplane.workspace import WorkspaceManager


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def setup_workspace(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.c").write_text("original", encoding="utf-8")
    manager = WorkspaceManager(tmp_path / "store")
    workspace = manager.create("run", str(source), tenant_id="tenant")
    manager.release(workspace.workspace_id)
    return manager, workspace, Path(workspace.path) / "main.c"


def test_snapshot_survives_restart_and_restores_only_requested_file(tmp_path):
    manager, workspace, target = setup_workspace(tmp_path)
    target.write_text("changed", encoding="utf-8")
    extra = target.parent / "user-new.txt"
    extra.write_text("keep", encoding="utf-8")
    manager = WorkspaceManager(manager.root)
    result = manager.restore_file(workspace.workspace_id, tenant_id="tenant",
                                  checkpoint_version=1, expected_version=1,
                                  relative="main.c", expected_hash=digest("changed"))
    assert target.read_text(encoding="utf-8") == "original"
    assert extra.read_text(encoding="utf-8") == "keep"
    assert result["after_hash"] == digest("original")
    # The user's newer content is also retained before rollback.
    assert len(manager.get(workspace.workspace_id).checkpoints) == 2


def test_restore_rejects_file_conflict_and_preserves_newer_changes(tmp_path):
    manager, workspace, target = setup_workspace(tmp_path)
    target.write_text("user-edited", encoding="utf-8")
    with pytest.raises(ValueError, match="version conflict"):
        manager.restore_file(workspace.workspace_id, tenant_id="tenant",
                             checkpoint_version=1, expected_version=1,
                             relative="main.c", expected_hash=digest("older"))
    assert target.read_text(encoding="utf-8") == "user-edited"
    assert manager.get(workspace.workspace_id).state == "released"


def test_restore_rejects_other_tenant_active_workspace_and_stale_checkpoint(tmp_path):
    manager, workspace, target = setup_workspace(tmp_path)
    args = dict(checkpoint_version=1, expected_version=1, relative="main.c", expected_hash=digest("original"))
    with pytest.raises(LookupError):
        manager.restore_file(workspace.workspace_id, tenant_id="other", **args)
    workspace.state = "active"
    manager._persist(workspace)
    with pytest.raises(ValueError, match="Stop"):
        manager.restore_file(workspace.workspace_id, tenant_id="tenant", **args)
    workspace.state = "released"
    manager._persist(workspace)
    manager.checkpoint(workspace.workspace_id, tenant_id="tenant")
    with pytest.raises(ValueError, match="Checkpoint version conflict"):
        manager.restore_file(workspace.workspace_id, tenant_id="tenant", **args)
    assert target.read_text(encoding="utf-8") == "original"


@pytest.mark.parametrize("relative", ["../outside", ".git/config", "C:/outside", "nested\\file"])
def test_restore_rejects_unsafe_paths(tmp_path, relative):
    manager, workspace, _ = setup_workspace(tmp_path)
    with pytest.raises(ValueError, match="path"):
        manager.restore_file(workspace.workspace_id, tenant_id="tenant", checkpoint_version=1,
                             expected_version=1, relative=relative, expected_hash=None)


def test_workspace_operation_lock_and_unverified_restart_fail_closed(tmp_path):
    manager, workspace, target = setup_workspace(tmp_path)
    lock = manager._metadata_path(workspace.workspace_id).with_suffix(".checkpoint.lock")
    lock.write_text("interrupted operation", encoding="utf-8")
    with pytest.raises(ValueError, match="crash verification"):
        manager.restore_file(workspace.workspace_id, tenant_id="tenant", checkpoint_version=1,
                             expected_version=1, relative="main.c", expected_hash=digest("original"))
    assert target.read_text(encoding="utf-8") == "original"
    lock.unlink()
    workspace.state = "active"
    manager._persist(workspace)
    restarted = WorkspaceManager(manager.root)
    with pytest.raises(ValueError, match="unknown execution"):
        restarted.restore_file(workspace.workspace_id, tenant_id="tenant", checkpoint_version=1,
                               expected_version=1, relative="main.c", expected_hash=digest("original"))


def test_checkpoint_routes_enforce_permissions_versions_and_tenant(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from forgeflow.api.routers import codeplane
    from forgeflow.auth.jwt import create_access_token
    from forgeflow.middleware.auth import RBACMiddleware
    from tests.business_auth import authenticate_business_app

    manager, workspace, target = setup_workspace(tmp_path)
    from forgeflow.runtime.orchestrator import mark_run_terminal, register_running_run
    record = register_running_run(run_id="run", thread_id="run", tenant_id="tenant",
                                  actor_user_id="checkpoint-user", intent="checkpoint", workflow_type="generic")
    record.codeplane = {"workspace": {"workspace_id": workspace.workspace_id}}
    mark_run_terminal("run", status="completed")
    monkeypatch.setattr(codeplane, "get_workspace_manager", lambda: manager)
    app = FastAPI()
    authenticate_business_app(app)
    app.add_middleware(RBACMiddleware)
    app.include_router(codeplane.router, prefix="/codeplane")
    client = TestClient(app)

    def auth(role="admin", tenant="tenant"):
        return {"Authorization": "Bearer " + create_access_token(user_id="checkpoint-user", role=role, workspace_id=tenant)}

    base = "/codeplane/workspaces/" + workspace.workspace_id
    assert client.get(base + "/checkpoints", headers=auth(tenant="other")).status_code == 404
    listing = client.get(base + "/checkpoints", headers=auth())
    assert listing.status_code == 200 and listing.json()["version"] == 1, listing.text
    target.write_text("changed", encoding="utf-8")
    detail = client.get(base + "/checkpoints/1", headers=auth())
    assert detail.status_code == 200
    assert detail.json()["files"] == [{"file": "main.c", "stored_hash": digest("original"),
                                      "current_hash": digest("changed"), "bytes": len("original")}]
    assert client.get(base + "/checkpoints/1", headers=auth(tenant="other")).status_code == 404
    body = {"checkpoint_version": 1, "expected_checkpoint_version": 1,
            "file": "main.c", "expected_current_hash": digest("changed")}
    assert client.post(base + "/restore-file", json=body, headers=auth("viewer")).status_code == 403
    assert client.post(base + "/restore-file", json=body, headers=auth(tenant="other")).status_code == 404
    result = client.post(base + "/restore-file", json=body, headers=auth())
    assert result.status_code == 200, result.text
    assert target.read_text(encoding="utf-8") == "original"
    assert client.post(base + "/restore-file", json=body, headers=auth()).status_code == 409


@pytest.mark.asyncio
async def test_restore_invalidates_old_commit_evidence_and_cross_tenant_access(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from forgeflow.runtime.tool_handlers import code_commit

    manager, workspace, target = setup_workspace(tmp_path)
    monkeypatch.setattr("forgeflow.codeplane.workspace.get_workspace_manager", lambda: manager)
    prior = {"workspace": workspace.to_dict(), "diff": "old diff"}
    target.write_text("changed", encoding="utf-8")
    manager.restore_file(workspace.workspace_id, tenant_id="tenant", checkpoint_version=1,
                         expected_version=1, relative="main.c", expected_hash=digest("changed"))
    args = {"approval": "approved", "workspace_id": workspace.workspace_id, "prior": prior}
    result = await code_commit(args, SimpleNamespace(tenant_id="tenant"))
    assert result["ok"] is False and "after validation" in result["reason"]
    result = await code_commit(args, SimpleNamespace(tenant_id="other"))
    assert result["ok"] is False and "current tenant" in result["reason"]


@pytest.mark.parametrize("use_zip", [False, True])
def test_workspace_does_not_copy_local_secrets_but_preserves_examples(tmp_path, use_zip):
    import io
    import zipfile

    source = tmp_path / "source"
    source.mkdir()
    files = {"main.py": "print('hello')", ".env": "SECRET=private",
             ".env.production": "SECRET=private", "cert.key": "private",
             ".env.example": "SECRET=replace-me"}
    for name, content in files.items():
        (source / name).write_text(content, encoding="utf-8")
    supplied = str(source)
    if use_zip:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        supplied = {"zip": buffer.getvalue()}
    workspace = WorkspaceManager(tmp_path / "store").create("private-copy", supplied, tenant_id="tenant")
    copied = Path(workspace.path)
    assert (copied / "main.py").exists() and (copied / ".env.example").exists()
    assert not any((copied / name).exists() for name in (".env", ".env.production", "cert.key"))
    assert (source / ".env").read_text(encoding="utf-8") == "SECRET=private"
