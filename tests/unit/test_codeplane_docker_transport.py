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


def test_gateway_cleanup_runs_after_cancellation(monkeypatch):
    from unittest.mock import AsyncMock

    import pytest

    engine = _build_engine(settings())
    monkeypatch.setattr(engine, "available", lambda: True)
    monkeypatch.setattr(engine, "_merge_defaults", lambda job: job)
    monkeypatch.setattr(engine, "_prepare_model_gateway", lambda job: None)
    cleanup = AsyncMock()
    monkeypatch.setattr(engine, "_cleanup_model_gateway", cleanup)
    monkeypatch.setattr(SubprocessOpenHandsEngine, "run", AsyncMock(side_effect=asyncio.CancelledError))
    job = CodeJob(run_id="cancelled")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(engine.run(job))
    cleanup.assert_awaited_once_with(job)


def test_gateway_setup_failure_never_falls_back_to_bridge(monkeypatch):
    from unittest.mock import AsyncMock

    engine = _build_engine(settings())
    monkeypatch.setattr(engine, "available", lambda: True)
    monkeypatch.setattr(engine, "_merge_defaults", lambda job: job)

    def fail_setup(job):
        raise OSError("gateway unavailable")

    monkeypatch.setattr(engine, "_prepare_model_gateway", fail_setup)
    cleanup = AsyncMock()
    monkeypatch.setattr(engine, "_cleanup_model_gateway", cleanup)
    runner = AsyncMock()
    monkeypatch.setattr(SubprocessOpenHandsEngine, "run", runner)
    job = CodeJob(run_id="failed")
    result = asyncio.run(engine.run(job))
    assert result.status == "unavailable"
    assert result.raw["isolation"]["network"] == "unavailable"
    runner.assert_not_awaited()
    cleanup.assert_awaited_once_with(job)


def test_container_mounts_only_workspace_and_readonly_runtime_and_git(tmp_path):
    workspace = WorkspaceManager(tmp_path / "store").create("run", tenant_id="tenant")
    engine = DockerOpenHandsEngine(settings=settings())
    job = CodeJob(run_id="run", workspace_path=workspace.path,
                  base_url="http://127.0.0.1:11434", test_command="host-python -m pytest -q")
    engine._networks[job.run_id] = ("internal-network", "proxy", "test-secret")
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
    assert command[command.index("--network") + 1] == "internal-network"
    assert mapped.base_url == "http://model-proxy:18765/test-secret"
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


def test_proxy_secret_is_removed_from_persisted_runner_events():
    import json

    engine = DockerOpenHandsEngine(settings=settings())
    engine._networks["run"] = ("network", "proxy", "test-secret")
    event = {"kind": "engine", "status": "ok", "data": {"base_url": "http://model-proxy/test-secret"}}
    result = engine._parse(json.dumps(event), "test-secret", 1, CodeJob(run_id="run"))
    assert "test-secret" not in repr(result.raw)
    assert "test-secret" not in repr(result.timeline)


def test_nonzero_runner_result_cannot_be_reported_as_success():
    import json

    engine = DockerOpenHandsEngine(settings=settings())
    frames = [
        {"kind": "diff", "status": "ok", "data": {"diff": "+changed"}},
        {"kind": "result", "status": "error", "data": {"exit_code": 1}},
    ]
    result = engine._parse("\n".join(map(json.dumps, frames)), "", 1, CodeJob())
    assert result.status == "error"
    assert engine._parse("\n".join(map(json.dumps, frames)), "", 0, CodeJob()).status == "error"
    assert engine._parse(json.dumps(frames[0]), "", 0, CodeJob()).status == "error"


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
