"""Phase 3 — the docker argv builder is the testable core of the sandbox.

Every hard limit must be present in the generated ``docker run`` argv
for every policy, and the forbidden weakenings (``seccomp=unconfined``,
``--privileged``, host network, docker-socket mounts, root user) must
never appear. These tests need no daemon: ``build_docker_argv`` is a
pure function.

Pre-fix evidence: on the base commit the unified builder does not
exist (import failure), and the legacy hand-rolled argv in
``docker_isolated.run_isolated_docker`` provably lacked ``--cap-drop``,
``no-new-privileges`` and ``--user`` (see the phase report's captured
argv) — the limits asserted here were NOT all enforced before.
"""

from __future__ import annotations

import pytest

from forgeflow.sandbox.docker_backend import DEFAULT_CONTAINER_USER, build_docker_argv
from forgeflow.sandbox.runtime import (
    PolicyDenied,
    SandboxLimits,
    SandboxPolicy,
    SandboxPolicySpec,
)

_LIMITS = SandboxLimits(
    cpu_cores=1.5,
    memory_bytes=268_435_456,
    pids_limit=48,
    file_size_bytes=8 * 1024 * 1024,
    tmpfs_bytes=32 * 1024 * 1024,
    timeout_s=12.0,
)


def _argv(spec: SandboxPolicySpec, **overrides: object) -> list[str]:
    kwargs: dict = {
        "image": "python:3.12-alpine",
        "spec": spec,
        "limits": _LIMITS,
        "workspace_host": "/srv/sbx/t1/run1/exec1",
        "tenant_id": "t1",
        "run_id": "run1",
        "container_name": "ffsbx-exec1",
        "command": ("python", "/workspace/_sandbox_main.py"),
        "env": {"LANG": "C.UTF-8", "PATH": "/usr/bin:/bin"},
    }
    kwargs.update(overrides)
    return build_docker_argv(**kwargs)


def _value(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def _mounts(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, tok in enumerate(argv) if tok == "--mount"]


@pytest.mark.parametrize(
    "spec",
    [
        SandboxPolicySpec.read_only(),
        SandboxPolicySpec.workspace_write(),
        SandboxPolicySpec.full_access(),
    ],
)
def test_argv_always_carries_the_full_hard_limit_set(spec: SandboxPolicySpec) -> None:
    argv = _argv(spec)
    # read-only root filesystem
    assert "--read-only" in argv
    # privilege stripping
    assert _value(argv, "--cap-drop") == "ALL"
    assert "no-new-privileges" in argv
    # non-root user, and never root
    user = _value(argv, "--user")
    assert user == DEFAULT_CONTAINER_USER
    assert user.split(":")[0] not in ("0", "root")
    # memory ceiling is a *pair* — a bare --memory leaves swap headroom
    assert _value(argv, "--memory") == str(_LIMITS.memory_bytes)
    assert _value(argv, "--memory-swap") == str(_LIMITS.memory_bytes)
    # cpu / pids / per-file size ceilings
    assert _value(argv, "--cpus") == str(_LIMITS.cpu_cores)
    assert _value(argv, "--pids-limit") == str(_LIMITS.pids_limit)
    assert _value(argv, "--ulimit") == (
        f"fsize={_LIMITS.file_size_bytes}:{_LIMITS.file_size_bytes}"
    )
    # size-capped, hardened /tmp
    tmpfs = _value(argv, "--tmpfs")
    assert tmpfs.startswith("/tmp:")
    assert f"size={_LIMITS.tmpfs_bytes}" in tmpfs
    assert "noexec" in tmpfs and "nosuid" in tmpfs
    # ephemeral + labelled for reaping
    assert "--rm" in argv
    assert "forgeflow.sandbox=true" in argv
    assert "forgeflow.tenant_id=t1" in argv
    assert "forgeflow.run_id=run1" in argv
    # the command runs the requested image + argv, nothing smuggled in
    image_at = argv.index("python:3.12-alpine")
    assert argv[image_at + 1 :] == ["python", "/workspace/_sandbox_main.py"]
    assert _value(argv, "--workdir") == "/workspace"


@pytest.mark.parametrize(
    "spec",
    [
        SandboxPolicySpec.read_only(),
        SandboxPolicySpec.workspace_write(),
        SandboxPolicySpec.full_access(),
        SandboxPolicySpec.full_access(open_network=True),
    ],
)
def test_argv_never_contains_a_weakening(spec: SandboxPolicySpec) -> None:
    argv = _argv(spec)
    joined = " ".join(argv)
    assert "--privileged" not in argv
    assert "seccomp=unconfined" not in joined
    assert "apparmor=unconfined" not in joined
    assert "/var/run/docker.sock" not in joined
    assert "docker.sock" not in joined
    if _value(argv, "--network") != "bridge":
        assert _value(argv, "--network") == "none"
    # no host namespaces of any kind
    for flag in ("--network", "--pid", "--ipc", "--uts"):
        if flag in argv:
            assert _value(argv, flag) != "host", (flag, argv)


def test_read_only_policy_mounts_workspace_ro() -> None:
    argv = _argv(SandboxPolicySpec.read_only())
    workspace_mounts = [m for m in _mounts(argv) if "target=/workspace" in m]
    assert len(workspace_mounts) == 1
    assert workspace_mounts[0].endswith(",ro")
    assert "source=/srv/sbx/t1/run1/exec1" in workspace_mounts[0]


@pytest.mark.parametrize("level", [SandboxPolicy.WORKSPACE_WRITE, SandboxPolicy.FULL_ACCESS])
def test_write_policies_mount_workspace_rw(level: SandboxPolicy) -> None:
    argv = _argv(SandboxPolicySpec(level=level))
    workspace_mounts = [m for m in _mounts(argv) if "target=/workspace" in m]
    assert workspace_mounts[0].endswith(",rw")


def test_default_no_network_at_every_level() -> None:
    for level in SandboxPolicy:
        argv = _argv(SandboxPolicySpec(level=level))
        assert _value(argv, "--network") == "none", level


def test_open_network_only_for_full_access_star() -> None:
    argv = _argv(SandboxPolicySpec.full_access(open_network=True))
    assert _value(argv, "--network") == "bridge"
    # "*" below FULL_ACCESS is an escalation the builder must refuse
    with pytest.raises(PolicyDenied):
        _argv(SandboxPolicySpec(level=SandboxPolicy.WORKSPACE_WRITE, network_allowlist=("*",)))
    with pytest.raises(PolicyDenied):
        _argv(SandboxPolicySpec(level=SandboxPolicy.READ_ONLY, network_allowlist=("*",)))


@pytest.mark.parametrize(
    "level", [SandboxPolicy.READ_ONLY, SandboxPolicy.WORKSPACE_WRITE, SandboxPolicy.FULL_ACCESS]
)
def test_specific_host_allowlist_is_refused_not_widened(level: SandboxPolicy) -> None:
    """Docker cannot filter egress by hostname: a specific allowlist
    must fail closed, never silently become open network."""
    with pytest.raises(PolicyDenied):
        _argv(SandboxPolicySpec(level=level, network_allowlist=("api.example.com",)))


def test_gvisor_runtime_only_when_requested() -> None:
    plain = _argv(SandboxPolicySpec.read_only())
    assert "--runtime" not in plain
    gvisor = _argv(SandboxPolicySpec.read_only(), runtime="runsc")
    assert _value(gvisor, "--runtime") == "runsc"


@pytest.mark.parametrize("user", ["0", "0:0", "root", "root:root", ""])
def test_root_user_is_refused(user: str) -> None:
    with pytest.raises(PolicyDenied):
        _argv(SandboxPolicySpec.read_only(), user=user)


def test_extra_ro_mounts_are_mounted_readonly() -> None:
    spec = SandboxPolicySpec(level=SandboxPolicy.READ_ONLY, extra_ro_mounts=("/data/reference",))
    argv = _argv(spec)
    extra = [m for m in _mounts(argv) if "/data/reference" in m]
    assert len(extra) == 1
    assert "readonly" in extra[0]


def test_env_pairs_are_emitted_sorted_and_complete() -> None:
    argv = _argv(SandboxPolicySpec.read_only(), env={"ZED": "1", "ALPHA": "2", "PATH": "/bin"})
    pairs = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--env"]
    assert pairs == ["ALPHA=2", "PATH=/bin", "ZED=1"]
