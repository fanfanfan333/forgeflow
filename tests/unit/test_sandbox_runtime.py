"""Phase 3 — the unified sandbox runtime: policy, workspaces, factory
gates, and the local process backend (real subprocesses, no docker).

The local-backend tests run under the explicit dev/test opt-in (the
fixture sets ``FORGEFLOW_SANDBOX_ALLOW_LOCAL=1``; the suite's conftest
already pins ``FORGEFLOW_SINGLE_PROCESS_DEV=1`` — the same discipline
as the run-lease process-local stores). Gate tests assert the factory
*refuses* construction when any leg of the opt-in is missing.

Pre-fix evidence: on the base commit none of this exists (import
failure); the behaviours pinned here (process-group timeout kill,
env non-inheritance, fail-closed factory) had no equivalent.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from forgeflow.sandbox.docker_backend import DockerBackend
from forgeflow.sandbox.docker_isolated import docker_isolation_available
from forgeflow.sandbox.runtime import (
    DEFAULT_LIMITS,
    DEFAULT_POLICY,
    LocalProcessBackend,
    PolicyDenied,
    RemoteBackend,
    SandboxConfigurationError,
    SandboxLimits,
    SandboxPolicy,
    SandboxPolicySpec,
    SandboxRequest,
    SandboxUnavailable,
    WorkspaceManager,
    build_sandbox_runtime,
    get_sandbox_runtime,
    reset_sandbox_runtime,
    sanitize_path_component,
    validate_requested_env,
)


def _settings(**overrides: object) -> SimpleNamespace:
    base: dict = {
        "sandbox_backend": "docker",
        "sandbox_image": "python:3.12-alpine",
        "sandbox_root": "",
        "sandbox_remote_endpoint": "",
        "environment": lambda: "dev",
        "resource_store_path": lambda: Path("/tmp/ff-sbx-test-root"),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture()
def local_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalProcessBackend:
    monkeypatch.setenv("FORGEFLOW_SANDBOX_ALLOW_LOCAL", "1")
    monkeypatch.setenv("FORGEFLOW_SINGLE_PROCESS_DEV", "1")
    return LocalProcessBackend(workspace_manager=WorkspaceManager(tmp_path / "sbx"))


# --------------------------------------------------------------------------- #
# Policy / limits value objects                                                 #
# --------------------------------------------------------------------------- #
def test_policy_defaults_are_read_only_and_offline() -> None:
    assert DEFAULT_POLICY.level is SandboxPolicy.READ_ONLY
    assert DEFAULT_POLICY.network_allowlist == ()
    assert SandboxPolicy.from_value("full_access") is SandboxPolicy.FULL_ACCESS
    assert SandboxPolicy.from_value("workspace_write") is SandboxPolicy.WORKSPACE_WRITE
    assert SandboxPolicy.from_value("god_mode") is None
    assert SandboxPolicy.from_value(None) is None
    assert (
        SandboxPolicy.READ_ONLY.rank
        < SandboxPolicy.WORKSPACE_WRITE.rank
        < SandboxPolicy.FULL_ACCESS.rank
    )


def test_limits_defaults_are_sane_and_single_sourced() -> None:
    assert DEFAULT_LIMITS.memory_bytes > 0
    assert DEFAULT_LIMITS.pids_limit > 0
    assert DEFAULT_LIMITS.timeout_s > 0
    assert DEFAULT_LIMITS.cpu_cores > 0


# --------------------------------------------------------------------------- #
# Environment discipline                                                        #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name",
    [
        "DATABASE_URL",
        "POSTGRES_URL",
        "FOO_SECRET",
        "GITHUB_TOKEN",
        "APP_PASSWORD",
        "AWS_ACCESS_KEY_ID",
        "OPENAI_API_KEY",
        "MY_PRIVATE_KEY",
    ],
)
def test_credential_shaped_env_names_are_refused(name: str) -> None:
    with pytest.raises(PolicyDenied):
        validate_requested_env({name: "x"})


def test_env_names_must_be_identifiers() -> None:
    with pytest.raises(PolicyDenied):
        validate_requested_env({"NOT A NAME": "x"})
    assert validate_requested_env({"MY_FLAG": "1"}) == {"MY_FLAG": "1"}


# --------------------------------------------------------------------------- #
# Workspace safety                                                              #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bad", ["../escape", "a/b", "a\\b", "", ".", "..", ".hidden", "white space", "a\x00b"]
)
def test_sanitize_path_component_rejects_traversal(bad: str) -> None:
    with pytest.raises(PolicyDenied):
        sanitize_path_component(bad, what="tenant")


def test_sanitize_path_component_accepts_real_id_shapes() -> None:
    assert sanitize_path_component("__forgeflow_system__", what="tenant")
    assert sanitize_path_component("tenant-1.2_x", what="tenant")


def test_workspace_layout_is_per_tenant_and_run(tmp_path: Path) -> None:
    wm = WorkspaceManager(tmp_path)
    ws_a = wm.prepare("tenant-a", "run-1")
    ws_b = wm.prepare("tenant-b", "run-1")
    assert ws_a.root != ws_b.root
    assert ws_a.root.parent.parent.name == "tenant-a"
    assert ws_b.root.parent.parent.name == "tenant-b"
    assert ws_a.root.is_dir() and ws_b.root.is_dir()
    # a traversal tenant id is refused before any directory exists
    with pytest.raises(PolicyDenied):
        wm.prepare("../tenant-b", "run-1")


def test_resolve_within_accepts_inside_and_rejects_escapes(tmp_path: Path) -> None:
    wm = WorkspaceManager(tmp_path)
    ws = wm.prepare("t1", "r1")
    inside = wm.resolve_within(ws, "sub/file.txt")
    assert inside == (ws.root / "sub" / "file.txt").resolve()
    assert (
        wm.resolve_within(ws, ws.root / "abs-inside.txt") == (ws.root / "abs-inside.txt").resolve()
    )
    with pytest.raises(PolicyDenied):
        wm.resolve_within(ws, "../outside.txt")
    with pytest.raises(PolicyDenied):
        wm.resolve_within(ws, "/etc/passwd")
    with pytest.raises(PolicyDenied):
        wm.resolve_within(ws, "sub/../../outside.txt")


def test_resolve_within_rejects_symlink_escape(tmp_path: Path) -> None:
    wm = WorkspaceManager(tmp_path)
    ws = wm.prepare("t1", "r1")
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = ws.root / "innocent-link"
    link.symlink_to(outside)
    with pytest.raises(PolicyDenied):
        wm.resolve_within(ws, "innocent-link")
    # a symlink that stays inside the workspace is fine
    real = ws.root / "real.txt"
    real.write_text("ok", encoding="utf-8")
    (ws.root / "inside-link").symlink_to(real)
    assert wm.resolve_within(ws, "inside-link") == real.resolve()


def test_workspace_cleanup_removes_tree(tmp_path: Path) -> None:
    wm = WorkspaceManager(tmp_path)
    ws = wm.prepare("t1", "r1")
    (ws.root / "f.txt").write_text("x", encoding="utf-8")
    assert wm.cleanup(ws) is True
    assert not ws.root.exists()


# --------------------------------------------------------------------------- #
# Factory gates (fail closed)                                                   #
# --------------------------------------------------------------------------- #
def test_factory_default_is_docker_matching_daemon_availability() -> None:
    """Environment-relative (the PR #7 CI failure): the default factory
    backend is docker, and its ``available()`` must agree with an
    independent daemon probe — False with no daemon (this dev VM:
    execute then fails closed) and True where a daemon answers (CI
    runners). The original test hard-coded the no-daemon outcome and
    failed on any docker host. The daemon-present branch asserts
    construction + availability only; container execution itself is
    covered by the docker-gated integration suite."""
    runtime = build_sandbox_runtime(_settings())
    assert isinstance(runtime, DockerBackend)
    daemon_up = docker_isolation_available()
    assert runtime.available() is daemon_up
    if not daemon_up:
        with pytest.raises(SandboxUnavailable):
            runtime.execute(SandboxRequest(code="print(1)"))


def test_factory_gvisor_requires_runsc_and_never_falls_back() -> None:
    runtime = build_sandbox_runtime(_settings(sandbox_backend="docker-gvisor"))
    assert isinstance(runtime, DockerBackend)
    assert runtime.name == "docker-gvisor"
    assert runtime.available() is False  # no daemon here at all


def test_factory_remote_is_a_fail_closed_stub() -> None:
    runtime = build_sandbox_runtime(_settings(sandbox_backend="remote"))
    assert isinstance(runtime, RemoteBackend)
    assert runtime.available() is False
    with pytest.raises(SandboxUnavailable):
        runtime.execute(SandboxRequest(argv=("echo", "hi")))


def test_factory_unknown_backend_is_refused() -> None:
    with pytest.raises(SandboxConfigurationError):
        build_sandbox_runtime(_settings(sandbox_backend="bare-metal-please"))


def test_factory_local_refused_without_the_dedicated_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FORGEFLOW_SANDBOX_ALLOW_LOCAL", raising=False)
    monkeypatch.setenv("FORGEFLOW_SINGLE_PROCESS_DEV", "1")
    with pytest.raises(SandboxConfigurationError):
        build_sandbox_runtime(_settings(sandbox_backend="local"))


def test_factory_local_refused_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGEFLOW_SANDBOX_ALLOW_LOCAL", "1")
    monkeypatch.setenv("FORGEFLOW_SINGLE_PROCESS_DEV", "1")
    prod = _settings(sandbox_backend="local", environment=lambda: "prod")
    with pytest.raises(SandboxConfigurationError):
        build_sandbox_runtime(prod)


def test_factory_local_refused_without_single_process_dev(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FORGEFLOW_SANDBOX_ALLOW_LOCAL", "1")
    monkeypatch.delenv("FORGEFLOW_SINGLE_PROCESS_DEV", raising=False)
    with pytest.raises(SandboxConfigurationError):
        build_sandbox_runtime(_settings(sandbox_backend="local"))


def test_factory_local_constructs_under_the_full_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FORGEFLOW_SANDBOX_ALLOW_LOCAL", "1")
    monkeypatch.setenv("FORGEFLOW_SINGLE_PROCESS_DEV", "1")
    runtime = build_sandbox_runtime(_settings(sandbox_backend="local"))
    assert isinstance(runtime, LocalProcessBackend)
    assert runtime.available() is True


def test_get_sandbox_runtime_caches_and_resets(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_sandbox_runtime()
    try:
        first = get_sandbox_runtime(_settings())
        second = get_sandbox_runtime(_settings(sandbox_backend="remote"))
        assert first is second  # process-wide, like the lease store
    finally:
        reset_sandbox_runtime()


# --------------------------------------------------------------------------- #
# Local backend — real subprocesses                                             #
# --------------------------------------------------------------------------- #
def test_local_argv_round_trip(local_backend: LocalProcessBackend) -> None:
    result = local_backend.execute(
        SandboxRequest(argv=("echo", "argv-ok"), tenant_id="t1", run_id="r1")
    )
    assert result.exit_code == 0
    assert result.stdout.strip() == "argv-ok"
    assert result.backend == "local"
    assert result.workspace_removed is True
    assert not Path(result.workspace_path).exists()


def test_local_code_round_trip_and_exit_code(local_backend: LocalProcessBackend) -> None:
    ok = local_backend.execute(
        SandboxRequest(code="print('sum', 2 + 3)", tenant_id="t1", run_id="r1")
    )
    assert ok.stdout.strip() == "sum 5"
    assert ok.exit_code == 0
    bad = local_backend.execute(
        SandboxRequest(code="import sys; sys.exit(3)", tenant_id="t1", run_id="r1")
    )
    assert bad.exit_code == 3
    assert bad.timed_out is False


def test_local_stdin_is_passed_through(local_backend: LocalProcessBackend) -> None:
    result = local_backend.execute(
        SandboxRequest(code="print('echo:' + input())", stdin="hello", tenant_id="t1", run_id="r1")
    )
    assert result.stdout.strip() == "echo:hello"


def test_local_timeout_kills_the_whole_process_group(
    local_backend: LocalProcessBackend,
) -> None:
    src = (
        "import subprocess, time\n"
        "child = subprocess.Popen(['sleep', '25'])\n"
        "open('gpid.txt', 'w').write(str(child.pid))\n"
        "time.sleep(25)\n"
    )
    started = time.monotonic()
    result = local_backend.execute(
        SandboxRequest(
            code=src,
            tenant_id="t1",
            run_id="r1",
            limits=SandboxLimits(timeout_s=1.0),
            keep_workspace=True,
        )
    )
    assert result.timed_out is True
    assert result.exit_code is None
    assert result.quota_exceeded == "timeout"
    assert time.monotonic() - started < 10
    if sys.platform.startswith("linux"):
        gpid = int((Path(result.workspace_path) / "gpid.txt").read_text())
        time.sleep(0.2)
        assert not os.path.exists(f"/proc/{gpid}"), "grandchild survived the group kill"


def test_local_host_environment_is_not_inherited(
    local_backend: LocalProcessBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FF_PHASE3_HOST_SECRET", "must-not-leak")
    result = local_backend.execute(
        SandboxRequest(
            code="import os; print(os.environ.get('FF_PHASE3_HOST_SECRET'))",
            tenant_id="t1",
            run_id="r1",
        )
    )
    assert result.stdout.strip() == "None"
    assert "must-not-leak" not in result.stdout


def test_local_requested_env_is_visible_but_credentials_are_refused(
    local_backend: LocalProcessBackend,
) -> None:
    result = local_backend.execute(
        SandboxRequest(
            code="import os; print(os.environ.get('MY_FLAG'))",
            env={"MY_FLAG": "yes"},
            tenant_id="t1",
            run_id="r1",
        )
    )
    assert result.stdout.strip() == "yes"
    with pytest.raises(PolicyDenied):
        local_backend.execute(
            SandboxRequest(
                code="print(1)",
                env={"DATABASE_URL": "postgres://prod"},
                tenant_id="t1",
                run_id="r1",
            )
        )


def test_local_code_network_is_blocked_by_the_guard(
    local_backend: LocalProcessBackend,
) -> None:
    src = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 80), timeout=2)\n"
        "    print('CONNECTED')\n"
        "except Exception:\n"
        "    print('BLOCKED')\n"
    )
    result = local_backend.execute(SandboxRequest(code=src, tenant_id="t1", run_id="r1"))
    assert "BLOCKED" in result.stdout
    assert "CONNECTED" not in result.stdout


def test_local_code_write_outside_workspace_is_blocked(
    local_backend: LocalProcessBackend, tmp_path: Path
) -> None:
    target = tmp_path / "escape-marker.txt"
    src = f"open({str(target)!r}, 'w').write('x')\nprint('WROTE')\n"
    result = local_backend.execute(SandboxRequest(code=src, tenant_id="t1", run_id="r1"))
    assert "WROTE" not in result.stdout
    assert not target.exists()


def test_local_code_write_inside_workspace_works(
    local_backend: LocalProcessBackend,
) -> None:
    src = "open('note.txt', 'w').write('hello')\nprint(open('note.txt').read())\n"
    result = local_backend.execute(
        SandboxRequest(
            code=src,
            policy=SandboxPolicySpec.workspace_write(),
            tenant_id="t1",
            run_id="r1",
        )
    )
    assert result.stdout.strip() == "hello"
    assert result.exit_code == 0


def test_local_disk_quota_is_measured_and_reported(
    local_backend: LocalProcessBackend,
) -> None:
    # An argv request stages nothing in the workspace, so the only
    # bytes measured are the ones the command itself writes.
    result = local_backend.execute(
        SandboxRequest(
            argv=(
                sys.executable,
                "-c",
                "open('big.bin', 'wb').write(b'x' * 8192); print('done')",
            ),
            policy=SandboxPolicySpec.workspace_write(),
            limits=SandboxLimits(disk_bytes=1024),
            tenant_id="t1",
            run_id="r1",
        )
    )
    assert result.stdout.strip() == "done"
    assert result.quota_exceeded == "disk"


def test_local_output_is_truncated_with_marker(local_backend: LocalProcessBackend) -> None:
    result = local_backend.execute(
        SandboxRequest(
            code="print('x' * 5000)",
            limits=SandboxLimits(max_output_chars=500),
            tenant_id="t1",
            run_id="r1",
        )
    )
    assert result.stdout_truncated is True
    assert len(result.stdout) <= 500


def test_local_request_cwd_is_created_inside_the_workspace(
    local_backend: LocalProcessBackend,
) -> None:
    result = local_backend.execute(
        SandboxRequest(
            code="import os; print(os.path.basename(os.getcwd()))",
            cwd="deep/nested",
            tenant_id="t1",
            run_id="r1",
        )
    )
    assert result.exit_code == 0
    assert result.stdout.strip() == "nested"
    # an escaping cwd is refused before anything runs
    with pytest.raises(PolicyDenied):
        local_backend.execute(
            SandboxRequest(code="print(1)", cwd="../outside", tenant_id="t1", run_id="r1")
        )


def test_local_keep_workspace_preserves_the_tree(local_backend: LocalProcessBackend) -> None:
    result = local_backend.execute(
        SandboxRequest(code="print(1)", tenant_id="t1", run_id="r1", keep_workspace=True)
    )
    assert result.workspace_removed is False
    assert Path(result.workspace_path).is_dir()


@pytest.mark.parametrize(
    "bad_request",
    [
        SandboxRequest(argv=("echo", "x"), code="print(1)"),
        SandboxRequest(),
        SandboxRequest(code="print(1)", language="ruby"),
        SandboxRequest(argv=()),
    ],
)
def test_request_validation_fails_closed(
    local_backend: LocalProcessBackend, bad_request: SandboxRequest
) -> None:
    with pytest.raises(PolicyDenied):
        local_backend.execute(bad_request)
