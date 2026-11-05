"""Phase 3 — sandbox Docker backend integration (docker-gated).

Every test here needs a real Docker daemon and **skips** without one
(the development VM for this phase has none; GitHub Actions ubuntu
runners do). A skip is the honest result — never a pass. The image is
the backend's pinned default (``python:3.12-alpine``); ``docker run``
pulls it on first use, as the legacy backend relied on.

Covered: echo round-trip, timeout kill (container gone afterwards),
OOM kill, network-none egress failure, READ_ONLY vs WORKSPACE_WRITE
workspace writes, read-only rootfs, workspace traversal containment,
and the non-root container user.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from forgeflow.sandbox.docker_backend import DockerBackend
from forgeflow.sandbox.runtime import (
    SandboxLimits,
    SandboxPolicySpec,
    SandboxRequest,
    WorkspaceManager,
)


@pytest.fixture(scope="module")
def backend(tmp_path_factory: pytest.TempPathFactory) -> DockerBackend:
    candidate = DockerBackend(
        workspace_manager=WorkspaceManager(tmp_path_factory.mktemp("sbx-root"))
    )
    if not candidate.available():
        pytest.skip("docker daemon unavailable — sandbox integration tests skip (never pass)")
    return candidate


def _run(backend: DockerBackend, code: str, **kw: object):
    return backend.execute(SandboxRequest(code=code, tenant_id="it-tenant", run_id="it-run", **kw))


def test_echo_round_trip(backend: DockerBackend) -> None:
    result = _run(backend, "print('integration-echo')")
    assert result.exit_code == 0
    assert result.stdout.strip() == "integration-echo"
    assert result.backend == "docker"
    assert result.workspace_removed is True


def test_timeout_kills_container(backend: DockerBackend) -> None:
    result = _run(
        backend,
        "import time; time.sleep(30)",
        limits=SandboxLimits(timeout_s=1.0),
    )
    assert result.timed_out is True
    assert result.exit_code is None
    # no sandbox container may survive the watchdog
    listed = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "label=forgeflow.sandbox=true"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert listed.stdout.strip() == ""


def test_oom_kill_is_detected(backend: DockerBackend) -> None:
    result = _run(
        backend,
        "a = []\nwhile True:\n    a.append('x' * (10 ** 6))\n",
        limits=SandboxLimits(memory_bytes=64 * 1024 * 1024, timeout_s=60.0),
    )
    assert result.exit_code == 137
    assert result.oom_killed is True
    assert result.quota_exceeded == "memory"


def test_network_none_blocks_egress(backend: DockerBackend) -> None:
    src = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 80), timeout=3)\n"
        "    print('CONNECTED')\n"
        "except Exception:\n"
        "    print('BLOCKED')\n"
    )
    result = _run(backend, src)
    assert "BLOCKED" in result.stdout
    assert "CONNECTED" not in result.stdout


def test_read_only_workspace_rejects_writes_but_write_policy_allows(
    backend: DockerBackend,
) -> None:
    src = "open('note.txt', 'w').write('x')\nprint('WROTE')\n"
    denied = _run(backend, src, policy=SandboxPolicySpec.read_only())
    assert "WROTE" not in denied.stdout
    assert denied.exit_code != 0
    allowed = _run(backend, src, policy=SandboxPolicySpec.workspace_write())
    assert allowed.exit_code == 0
    assert "WROTE" in allowed.stdout


def test_rootfs_is_read_only(backend: DockerBackend) -> None:
    result = _run(
        backend,
        "try:\n"
        "    open('/usr/local/escape', 'w').write('x')\n"
        "    print('WROTE-ROOT')\n"
        "except OSError:\n"
        "    print('ROOTFS-RO')\n",
        policy=SandboxPolicySpec.workspace_write(),
    )
    assert "ROOTFS-RO" in result.stdout
    assert "WROTE-ROOT" not in result.stdout


def test_container_user_is_not_root(backend: DockerBackend) -> None:
    result = backend.execute(
        SandboxRequest(argv=("id", "-u"), tenant_id="it-tenant", run_id="it-run")
    )
    assert result.exit_code == 0
    assert result.stdout.strip() != "0"


def test_workspace_traversal_cannot_reach_host_files(
    backend: DockerBackend, tmp_path: Path
) -> None:
    sentinel = tmp_path / "host-sentinel.txt"
    sentinel.write_text("host-only", encoding="utf-8")
    # From inside the container the workspace's parent (the sandbox
    # root tree on the host) is not mounted at all: ../.. leads to the
    # container's own root, where no host sentinel exists.
    src = (
        "import os\n"
        "print('sentinel-visible', os.path.exists('/host-sentinel.txt'))\n"
        "print('parent-listing', sorted(os.listdir('/')))\n"
    )
    result = _run(backend, src, policy=SandboxPolicySpec.workspace_write())
    assert result.exit_code == 0
    assert "sentinel-visible False" in result.stdout
    assert "host-sentinel" not in result.stdout


def test_docker_cli_is_the_pinned_image(backend: DockerBackend) -> None:
    assert backend.image == "python:3.12-alpine"
    assert shutil.which("docker") is not None
