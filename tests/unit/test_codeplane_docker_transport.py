import asyncio
from types import SimpleNamespace

from forgeflow.codeplane.docker_engine import DockerOpenHandsEngine
from forgeflow.codeplane.engine import CodeJob, SubprocessOpenHandsEngine, _build_engine
from forgeflow.codeplane.workspace import WorkspaceManager


def settings():
    return SimpleNamespace(codeplane_transport="docker", codeplane_enabled=True,
                           codeplane_docker_image="forgeflow-codeplane:1.49.6",
                           codeplane_test_command="host-python -m pytest -q",
                           codeplane_docker_test_command="python -m pytest -q")


def test_docker_transport_is_explicit_and_never_falls_back(monkeypatch):
    engine = _build_engine(settings())
    assert isinstance(engine, DockerOpenHandsEngine)
    monkeypatch.setattr(engine, "available", lambda: False)
    result = asyncio.run(engine.run(CodeJob()))
    assert result.status == "unavailable" and result.transport == "docker"
    assert result.raw["isolation"]["host_runtime_fallback"] is False


def test_container_mounts_only_workspace_and_readonly_runtime_and_git(tmp_path):
    workspace = WorkspaceManager(tmp_path / "store").create("run", tenant_id="tenant")
    engine = DockerOpenHandsEngine(settings=settings())
    job = CodeJob(run_id="run", workspace_path=workspace.path,
                  base_url="http://127.0.0.1:11434", test_command="host-python -m pytest -q")
    command, mapped = engine._launch(job)
    assert "--pull=never" in command and "--read-only" in command
    assert "--cap-drop=ALL" in command and "--security-opt=no-new-privileges" in command
    assert "--memory=4g" in command and "--pids-limit=256" in command
    mounts = [command[i + 1] for i, value in enumerate(command) if value == "--mount"]
    assert len(mounts) == 3
    assert any("target=/workspace/.git,readonly" in m for m in mounts)
    assert any("target=/opt/forgeflow-runner,readonly" in m for m in mounts)
    assert all("docker.sock" not in m for m in mounts)
    assert mapped.workspace_path == "/workspace"
    assert mapped.base_url == "http://host.docker.internal:11434"
    assert mapped.test_command == "python -m pytest -q"
    assert job.workspace_path == workspace.path


def test_other_transports_retain_existing_selection():
    assert isinstance(_build_engine(SimpleNamespace(codeplane_transport="subprocess")), SubprocessOpenHandsEngine)


def test_baseline_failures_are_distinct_from_post_edit_test_verdict():
    import json

    engine = DockerOpenHandsEngine(settings=settings())
    frames = [
        {"kind": "baseline_test", "status": "error", "data": {"raw_stdout": "1 failed", "exit_code": 1}},
        {"kind": "test", "status": "ok", "data": {"raw_stdout": "1 passed", "exit_code": 0}},
        {"kind": "result", "status": "ok", "data": {"exit_code": 0}},
    ]
    result = engine._parse("\n".join(json.dumps(frame) for frame in frames), "", 0, CodeJob(test_command="pytest -q"))
    assert result.raw["baseline_tests"]["verdict"] == "failed"
    assert result.tests.verdict == "passed"
    assert result.degraded is None


def test_failed_cleanup_does_not_confuse_daemon_outage_with_missing_container(monkeypatch):
    import subprocess

    from forgeflow.codeplane.docker_engine import ContainerCleanupUnverified

    engine = DockerOpenHandsEngine(settings=settings())
    engine._containers["run"] = "forgeflow-code-test"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=b"", stderr=b"daemon unavailable"))
    import pytest
    with pytest.raises(ContainerCleanupUnverified):
        asyncio.run(engine._cleanup(CodeJob(run_id="run")))
    assert engine._containers["run"] == "forgeflow-code-test"
