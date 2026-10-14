import json

import pytest

from forgeflow.codeplane.workspace import WorkspaceManager


def test_released_workspace_survives_manager_restart(tmp_path):
    manager = WorkspaceManager(tmp_path)
    workspace = manager.create("run-one", tenant_id="tenant")
    manager.release(workspace.workspace_id)
    restarted = WorkspaceManager(tmp_path)
    restored = restarted.get(workspace.workspace_id)
    assert restored is not None
    assert restored.tenant_id == "tenant"
    assert restored.state == "released"
    assert restored.path == workspace.path
    assert restarted.reuse("run-one") is restored


def test_restarted_active_workspace_requires_verification(tmp_path):
    workspace = WorkspaceManager(tmp_path).create("run-two", tenant_id="tenant")
    restarted = WorkspaceManager(tmp_path)
    restored = restarted.get(workspace.workspace_id)
    assert restored.state == "recovery_required"
    assert restarted.reuse("run-two") is None


@pytest.mark.parametrize("identifier", ["../escape", "..", "a/b", "a\\b"])
def test_metadata_lookup_rejects_path_traversal(tmp_path, identifier):
    assert WorkspaceManager(tmp_path).get(identifier) is None


def test_corrupt_or_outside_workspace_metadata_is_not_loaded(tmp_path):
    manager = WorkspaceManager(tmp_path)
    workspace = manager.create("run-three", tenant_id="tenant")
    metadata = manager._metadata_path(workspace.workspace_id)
    document = json.loads(metadata.read_text(encoding="utf-8"))
    document["workspace"]["path"] = str(tmp_path.parent)
    metadata.write_text(json.dumps(document), encoding="utf-8")
    assert WorkspaceManager(tmp_path).get(workspace.workspace_id) is None
    metadata.write_text("not json", encoding="utf-8")
    assert WorkspaceManager(tmp_path).get(workspace.workspace_id) is None
