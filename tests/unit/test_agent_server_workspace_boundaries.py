import threading

import pytest
from starlette.testclient import TestClient

from forgeflow.codeplane.runner.agent_server import app as server
from forgeflow.codeplane.runner.agent_server.models import BashOutput


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.2"])
def test_launcher_rejects_public_unauthenticated_execution(monkeypatch, host):
    from forgeflow.codeplane.runner.agent_server import __main__ as launcher

    monkeypatch.setenv("SESSION_API_KEY", "")
    monkeypatch.setattr("sys.argv", ["agent-server", "--host", host])
    with pytest.raises(SystemExit) as error:
        launcher.main()
    assert error.value.code == 2


def test_git_diff_rejects_foreign_path_before_reading(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    called = []
    monkeypatch.setattr(server, "_git_diff_for_path", lambda *args: called.append(args))
    with TestClient(server.create_app(token="test", workspace_root=str(workspace))) as client:
        response = client.get(
            "/api/git/diff",
            params={"path": str(tmp_path / "private.txt")},
            headers={"X-Session-API-Key": "test"},
        )
    assert response.status_code == 422
    assert not called


def test_bash_defaults_to_workspace_and_does_not_block_other_requests(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    working_dirs = []

    def run(request):
        working_dirs.append(request.cwd)
        entered.set()
        assert release.wait(5)
        return BashOutput(
            id="test", command_id="test", order=0, exit_code=0, stdout="", stderr="", timeout=False
        )

    monkeypatch.setattr(server, "_run_bash", run)
    with TestClient(server.create_app(token="test", workspace_root=str(tmp_path))) as client:
        result = []
        worker = threading.Thread(
            target=lambda: result.append(
                client.post(
                    "/api/bash/execute_bash_command",
                    json={"command": "test"},
                    headers={"X-Session-API-Key": "test"},
                )
            )
        )
        worker.start()
        try:
            assert entered.wait(2)
            assert (
                client.get(
                    "/api/conversations/count", headers={"X-Session-API-Key": "test"}
                ).status_code
                == 200
            )
        finally:
            release.set()
            worker.join(5)
        assert result[0].status_code == 200
    assert working_dirs == [str(tmp_path)]
