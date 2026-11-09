"""Phase 3 hardening round — spec items 0/2/3/4/5/6 regression pins.

* Item 0 — the container user is resolved from the effective host
  uid (one place: ``docker_backend.resolve_container_user``); no
  ``0o777``, no bare chown — workspace dirs are ``0o700``.
* Item 2 — ``code.execute`` under a stamped sandbox policy refuses
  the bare host subprocess engine unless the local dev/test opt-in
  holds (no silent host-side execution path).
* Item 3 — the local backend: fork/PID discipline + per-root flock
  claim; READ_ONLY really prevents writes (interpreter deny-all +
  ``RLIMIT_FSIZE=0``); network semantics aligned with docker
  (default none — netns for argv, guard+netns for code; a
  hostname allowlist is refused, never silently opened).
* Item 4 — stdout/stderr capture is bounded while streaming; the
  disk quota is enforced *during* the run, not only measured after.
* Item 5 — timeout reaps the whole process tree, including a
  grandchild that escaped the process group via ``setsid``.
* Item 6 — the workspace chain is created level-by-level without
  following symlinks; a planted symlink or foreign owner anywhere
  in root → tenant → run → leaf fails closed.

Pre-fix evidence is noted per test: the behaviours pinned here did
not exist (or were inverted) on the pre-hardening head ``d6376d9``.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import forgeflow.sandbox.docker_backend as docker_backend
from forgeflow.sandbox.docker_backend import DEFAULT_CONTAINER_USER, DockerBackend
from forgeflow.sandbox.runtime import (
    LocalProcessBackend,
    PolicyDenied,
    SandboxConfigurationError,
    SandboxLimits,
    SandboxPolicy,
    SandboxPolicySpec,
    SandboxRequest,
    WorkspaceManager,
    _netns_prefix,
)


@pytest.fixture()
def local_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalProcessBackend:
    monkeypatch.setenv("FORGEFLOW_SANDBOX_ALLOW_LOCAL", "1")
    monkeypatch.setenv("FORGEFLOW_SINGLE_PROCESS_DEV", "1")
    return LocalProcessBackend(workspace_manager=WorkspaceManager(tmp_path / "sbx"))


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --------------------------------------------------------------------------- #
# Item 0 — container user resolution + workspace modes                          #
# --------------------------------------------------------------------------- #
def test_container_user_is_the_host_uid_on_a_non_root_posix_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "getuid", lambda: 1001)
    monkeypatch.setattr(os, "getgid", lambda: 1001)
    assert docker_backend.resolve_container_user() == "1001:1001"


def test_container_user_falls_back_to_nobody_for_a_root_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "getuid", lambda: 0)
    monkeypatch.setattr(os, "getgid", lambda: 0)
    assert docker_backend.resolve_container_user() == DEFAULT_CONTAINER_USER == "65534:65534"


def test_docker_backend_default_user_comes_from_the_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "getuid", lambda: 1001)
    monkeypatch.setattr(os, "getgid", lambda: 1002)
    backend = DockerBackend(workspace_manager=WorkspaceManager(tmp_path / "sbx"))
    assert backend._user == "1001:1002"
    argv = docker_backend.build_docker_argv(
        image="python:3.12-alpine",
        spec=SandboxPolicySpec.workspace_write(),
        limits=SandboxLimits(),
        workspace_host=str(tmp_path),
        tenant_id="t1",
        run_id="r1",
        container_name="ffsbx-x",
        command=("python", "-V"),
        env={},
        user=docker_backend.resolve_container_user(),
    )
    assert argv[argv.index("--user") + 1] == "1001:1002"


def test_workspace_chain_directories_are_0700(tmp_path: Path) -> None:
    root = tmp_path / "sbx-root"
    wm = WorkspaceManager(root)
    ws = wm.prepare("tenant-a", "run-1")
    assert _mode(root) == 0o700
    assert _mode(root / "tenant-a") == 0o700
    assert _mode(root / "tenant-a" / "run-1") == 0o700
    assert _mode(ws.root) == 0o700


# --------------------------------------------------------------------------- #
# Item 6 — no ancestor symlink escape                                           #
# --------------------------------------------------------------------------- #
def test_symlink_at_the_tenant_level_is_refused_and_writes_nothing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sbx-root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "tenant-x").symlink_to(outside, target_is_directory=True)
    wm = WorkspaceManager(root)
    with pytest.raises(SandboxConfigurationError):
        wm.prepare("tenant-x", "run-1")
    assert list(outside.iterdir()) == [], "the escape target must stay untouched"


def test_symlink_at_the_run_level_is_refused(tmp_path: Path) -> None:
    wm = WorkspaceManager(tmp_path / "sbx-root")
    ws = wm.prepare("tenant-a", "run-1")
    outside = tmp_path / "outside"
    outside.mkdir()
    (ws.root.parent.parent / "run-evil").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SandboxConfigurationError):
        wm.prepare("tenant-a", "run-evil")
    assert list(outside.iterdir()) == []


def test_symlinked_sandbox_root_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real-root"
    real.mkdir()
    link = tmp_path / "link-root"
    link.symlink_to(real, target_is_directory=True)
    wm = WorkspaceManager(link)
    with pytest.raises(SandboxConfigurationError):
        wm.prepare("tenant-a", "run-1")
    assert list(real.iterdir()) == []


@pytest.mark.skipif(os.name != "posix" or os.geteuid() != 0, reason="chown needs root")
def test_foreign_owned_chain_component_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "sbx-root"
    tenant_dir = root / "tenant-foreign"
    tenant_dir.mkdir(parents=True)
    os.chown(tenant_dir, 65534, 65534)
    wm = WorkspaceManager(root)
    with pytest.raises(SandboxConfigurationError):
        wm.prepare("tenant-foreign", "run-1")


# --------------------------------------------------------------------------- #
# Item 3 — local backend process discipline                                     #
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork discipline is POSIX")
def test_local_backend_refuses_use_from_a_forked_child(
    local_backend: LocalProcessBackend,
) -> None:
    pid = os.fork()
    if pid == 0:  # child: the inherited backend must refuse to serve
        try:
            local_backend.execute(SandboxRequest(argv=("echo", "x"), tenant_id="t1", run_id="r1"))
        except SandboxConfigurationError:
            os._exit(0)
        except BaseException:
            os._exit(2)
        os._exit(1)
    _pid, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    # …and the parent backend is unaffected by the child's attempt.
    result = local_backend.execute(
        SandboxRequest(argv=("echo", "parent-ok"), tenant_id="t1", run_id="r1")
    )
    assert result.stdout.strip() == "parent-ok"


def test_local_root_claim_refuses_a_second_process(tmp_path: Path) -> None:
    root = tmp_path / "sbx-claim"
    repo_root = Path(__file__).resolve().parents[2]
    holder_src = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from forgeflow.sandbox.runtime import _claim_local_root\n"
        "_claim_local_root(Path(sys.argv[1]))\n"
        "print('claimed', flush=True)\n"
        "time.sleep(30)\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_src, str(root)],
        cwd=str(repo_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "claimed"
        with pytest.raises(SandboxConfigurationError):
            LocalProcessBackend(workspace_manager=WorkspaceManager(root))
    finally:
        holder.terminate()
        holder.wait(timeout=10)


# --------------------------------------------------------------------------- #
# Item 3 — local READ_ONLY really prevents writes                                #
# --------------------------------------------------------------------------- #
def test_local_read_only_code_cannot_write_inside_the_workspace(
    local_backend: LocalProcessBackend,
) -> None:
    src = (
        "try:\n"
        "    open('note.txt', 'w').write('x')\n"
        "    print('WROTE')\n"
        "except PermissionError:\n"
        "    print('DENIED')\n"
    )
    result = local_backend.execute(
        SandboxRequest(code=src, policy=SandboxPolicySpec.read_only(), tenant_id="t1", run_id="r1")
    )
    assert "DENIED" in result.stdout
    assert "WROTE" not in result.stdout


def test_local_read_only_argv_cannot_land_bytes(
    local_backend: LocalProcessBackend,
) -> None:
    src = (
        "try:\n"
        "    with open('payload.bin', 'wb') as fh:\n"
        "        fh.write(b'secret-bytes')\n"
        "    print('WROTE')\n"
        "except OSError:\n"
        "    print('FAILED')\n"
    )
    result = local_backend.execute(
        SandboxRequest(
            argv=(sys.executable, "-c", src),
            policy=SandboxPolicySpec.read_only(),
            tenant_id="t1",
            run_id="r1",
            keep_workspace=True,
        )
    )
    landed = Path(result.workspace_path) / "payload.bin"
    # The kernel ceiling (RLIMIT_FSIZE=0) is the guarantee: whatever
    # the payload believed happened, no bytes may have landed.
    assert not landed.exists() or landed.read_bytes() == b""


def test_local_workspace_write_argv_can_write(
    local_backend: LocalProcessBackend,
) -> None:
    result = local_backend.execute(
        SandboxRequest(
            argv=(sys.executable, "-c", "open('ok.txt', 'w').write('yes')"),
            policy=SandboxPolicySpec.workspace_write(),
            tenant_id="t1",
            run_id="r1",
            keep_workspace=True,
        )
    )
    assert result.exit_code == 0
    assert (Path(result.workspace_path) / "ok.txt").read_text() == "yes"


# --------------------------------------------------------------------------- #
# Item 3 — local network semantics, docker-aligned                              #
# --------------------------------------------------------------------------- #
def test_local_argv_default_policy_cannot_open_a_connection(
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
    if _netns_prefix() is None:
        # No namespace mechanism on this host ⇒ the argv payload must
        # be refused outright (fail closed), never run with open net.
        with pytest.raises(PolicyDenied):
            local_backend.execute(
                SandboxRequest(argv=(sys.executable, "-c", src), tenant_id="t1", run_id="r1")
            )
        return
    result = local_backend.execute(
        SandboxRequest(argv=(sys.executable, "-c", src), tenant_id="t1", run_id="r1")
    )
    assert "BLOCKED" in result.stdout
    assert "CONNECTED" not in result.stdout


@pytest.mark.parametrize("as_code", [True, False])
def test_local_specific_host_allowlist_is_refused_not_opened(
    local_backend: LocalProcessBackend, as_code: bool
) -> None:
    """Pre-fix, a code payload with a non-empty allowlist ran with
    the guard OFF (fully open network). A local process cannot
    filter by hostname: the only honest answers are refusal (here)
    or FULL_ACCESS + ('*',)."""
    request = SandboxRequest(
        policy=SandboxPolicySpec(
            level=SandboxPolicy.WORKSPACE_WRITE, network_allowlist=("api.example.com",)
        ),
        tenant_id="t1",
        run_id="r1",
        **({"code": "print(1)"} if as_code else {"argv": ("echo", "hi")}),
    )
    with pytest.raises(PolicyDenied):
        local_backend.execute(request)


def test_local_open_network_marker_below_full_access_is_refused(
    local_backend: LocalProcessBackend,
) -> None:
    with pytest.raises(PolicyDenied):
        local_backend.execute(
            SandboxRequest(
                code="print(1)",
                policy=SandboxPolicySpec(
                    level=SandboxPolicy.WORKSPACE_WRITE, network_allowlist=("*",)
                ),
                tenant_id="t1",
                run_id="r1",
            )
        )


def test_local_full_access_open_network_runs_unblocked(
    local_backend: LocalProcessBackend,
) -> None:
    result = local_backend.execute(
        SandboxRequest(
            code="print('ran-open')",
            policy=SandboxPolicySpec.full_access(open_network=True),
            tenant_id="t1",
            run_id="r1",
        )
    )
    assert result.exit_code == 0
    assert result.stdout.strip() == "ran-open"


# --------------------------------------------------------------------------- #
# Item 4 — bounded output + runtime disk enforcement                            #
# --------------------------------------------------------------------------- #
def test_local_output_capture_is_bounded_while_streaming(
    local_backend: LocalProcessBackend,
) -> None:
    result = local_backend.execute(
        SandboxRequest(
            code="print('x' * 2_000_000)",
            limits=SandboxLimits(max_output_chars=4096, timeout_s=30.0),
            tenant_id="t1",
            run_id="r1",
        )
    )
    assert result.exit_code == 0
    assert result.stdout_truncated is True
    assert len(result.stdout) <= 4096


def test_local_disk_limit_kills_during_the_run(local_backend: LocalProcessBackend) -> None:
    src = (
        "import time\n"
        "with open('fill.bin', 'wb') as fh:\n"
        "    for _ in range(200):\n"
        "        fh.write(b'x' * 4096)\n"
        "        fh.flush()\n"
        "        time.sleep(0.05)\n"
        "print('FINISHED')\n"
    )
    result = local_backend.execute(
        SandboxRequest(
            argv=(sys.executable, "-c", src),
            policy=SandboxPolicySpec.workspace_write(),
            limits=SandboxLimits(disk_bytes=64 * 1024, timeout_s=60.0),
            tenant_id="t1",
            run_id="r1",
            keep_workspace=True,
        )
    )
    # Killed mid-run by the runtime monitor — not a completed run
    # that a post-hoc measurement merely flags (the pre-fix shape).
    assert result.quota_exceeded == "disk"
    assert result.exit_code is None
    assert "FINISHED" not in result.stdout
    total = sum(p.stat().st_size for p in Path(result.workspace_path).rglob("*") if p.is_file())
    assert total < 200 * 4096  # the full payload never landed
    assert total <= 64 * 1024 + 128 * 1024  # budget + stated polling slack


# --------------------------------------------------------------------------- #
# Item 5 — timeout reaps the whole tree (setsid escape included)                #
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(os.name != "posix", reason="setsid/process-tree semantics are POSIX")
def test_local_timeout_reaps_a_setsid_grandchild(
    local_backend: LocalProcessBackend, tmp_path: Path
) -> None:
    marker = tmp_path / "grandchild-marker.txt"
    grandchild_src = f"import time\ntime.sleep(2.5)\nopen({str(marker)!r}, 'w').write('escaped')\n"
    src = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {grandchild_src!r}], start_new_session=True)\n"
        "time.sleep(30)\n"
    )
    result = local_backend.execute(
        SandboxRequest(
            code=src,
            policy=SandboxPolicySpec.workspace_write(),
            limits=SandboxLimits(timeout_s=1.0),
            tenant_id="t1",
            run_id="r1",
        )
    )
    assert result.timed_out is True
    # The grandchild escaped the payload's process group via setsid;
    # only descendant discovery + individual kills reap it. Give its
    # delayed write every chance to land, then prove it never did.
    time.sleep(3.5)
    assert not marker.exists()


# --------------------------------------------------------------------------- #
# Item 2 — code.execute transport compliance                                    #
# --------------------------------------------------------------------------- #
def _code_ctx(policy: str | None) -> SimpleNamespace:
    ctx = SimpleNamespace(
        run_id="run-tx",
        step_id="run-tx:0:0",
        tenant_id="t1",
        user_id="u1",
        role="admin",
        intent="transport probe",
        task_context=None,
    )
    if policy is not None:
        ctx.sandbox_policy = policy
    return ctx


@pytest.mark.asyncio
async def test_code_execute_refuses_the_real_host_engine_without_local_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from forgeflow.codeplane.engine import SubprocessOpenHandsEngine
    from forgeflow.runtime.tool_handlers import code_execute

    engine = SubprocessOpenHandsEngine(
        interpreter="/nonexistent/python", settings=SimpleNamespace()
    )
    monkeypatch.setattr("forgeflow.codeplane.engine.get_code_engine", lambda: engine)
    monkeypatch.setattr("forgeflow.sandbox.runtime.local_sandbox_allowed", lambda: False)
    result = await code_execute({"intent": "do something"}, _code_ctx("workspace_write"))
    assert result["ok"] is False
    assert result["unavailable"] is True
    assert "合规沙箱传输" in result["reason"]


@pytest.mark.asyncio
async def test_code_execute_host_engine_flows_when_local_opt_in_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the explicit dev/test opt-in the host engine is the
    sanctioned local substrate: the transport rule steps aside and
    the pre-existing engine flow decides (here: unavailable
    interpreter ⇒ the honest engine-unavailable degradation)."""
    from forgeflow.codeplane.engine import SubprocessOpenHandsEngine
    from forgeflow.runtime.tool_handlers import code_execute

    engine = SubprocessOpenHandsEngine(
        interpreter="/nonexistent/python", settings=SimpleNamespace()
    )
    monkeypatch.setattr("forgeflow.codeplane.engine.get_code_engine", lambda: engine)
    monkeypatch.setattr("forgeflow.sandbox.runtime.local_sandbox_allowed", lambda: True)
    result = await code_execute({"intent": "do something"}, _code_ctx("workspace_write"))
    assert result["ok"] is False
    assert result["unavailable"] is True
    assert "合规沙箱传输" not in result["reason"]
    assert "解释器不存在" in result["reason"]
