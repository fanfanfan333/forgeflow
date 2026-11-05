"""Phase 3 — the Docker sandbox backend (default), incl. gVisor ``runsc``.

The heart of this module is :func:`build_docker_argv` — a **pure**
function from (image, policy, limits, workspace, env) to the exact
``docker run`` argv, so every hard limit is unit-testable without a
daemon. The backend then only: probes availability, runs the argv
under a wall-clock watchdog (``docker kill`` on expiry — killing the
container kills the whole process tree, the container *is* the
process group), and reaps residue.

Hard limits, always emitted (never policy-optional):

* ``--read-only`` root filesystem; the workspace bind mount is the
  only writable area (``readonly=true`` at ``READ_ONLY``, no marker
  otherwise — ``--mount`` has no bare ``rw`` field) plus a
  size-capped ``/tmp`` tmpfs (``noexec,nosuid,nodev``);
* ``--cap-drop ALL``; ``--security-opt no-new-privileges``; a non-root
  ``--user`` (``65534:65534`` / nobody by default);
* ``--memory`` **and** ``--memory-swap`` set to the same value (a bare
  ``--memory`` silently leaves swap headroom);
* ``--cpus``; ``--pids-limit``; ``--ulimit fsize`` (single-file cap);
* ``--network none`` unless the policy's egress terms say otherwise
  (below); unique ``--name`` + tenant/run ``--label``\\ s for reaping;
  ``--rm`` plus an explicit ``docker rm -f`` residue sweep.

seccomp: the builder **never** emits ``seccomp=unconfined`` (or any
``--security-opt seccomp=…`` override) — containers run under Docker's
default seccomp profile. No ``--privileged``, no host network, no
docker-socket mount, ever; the unit tests pin the absence.

Network egress honesty: Docker cannot filter egress by hostname.
The rule implemented here (fail closed, never silently widened):

* empty allowlist ⇒ ``--network none`` (every policy level);
* allowlist ``("*",)`` **and** ``FULL_ACCESS`` ⇒ ``--network bridge``
  (open egress was explicitly requested at the top policy level,
  which itself requires a human approval upstream);
* anything else ⇒ :class:`PolicyDenied` — a specific-host allowlist
  needs an egress-enforcing substrate (a future remote backend /
  proxy setup), and this backend refuses rather than approximating
  it with open network.

Disk quota: a bind-mounted workspace cannot be hard-capped by
``docker run`` (``--storage-opt size`` governs the container's own
writable layer, which is empty under ``--read-only``). The enforced
pieces are the ``/tmp`` tmpfs cap, the per-file ``fsize`` ulimit, and
the runtime's post-execution workspace measurement
(``quota_exceeded="disk"``). That boundary is stated, not hidden.

gVisor: the same backend with ``runtime="runsc"``. Availability
additionally probes the daemon's runtime list; a host without runsc
reports unavailable — the backend **never** silently falls back to
``runc`` (that would silently weaken the isolation the operator
configured).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from forgeflow.sandbox.runtime import (
    OPEN_NETWORK,
    BaseSandboxRuntime,
    PolicyDenied,
    SandboxLimits,
    SandboxPolicy,
    SandboxPolicySpec,
    SandboxRequest,
    SandboxResult,
    SandboxWorkspace,
    WorkspaceManager,
    container_env,
)

__all__ = [
    "DEFAULT_CONTAINER_USER",
    "DockerBackend",
    "build_docker_argv",
]

#: Non-root identity containers run as (nobody:nogroup). Never "0".
DEFAULT_CONTAINER_USER = "65534:65534"

_WORKSPACE_TARGET = "/workspace"


def build_docker_argv(
    *,
    image: str,
    spec: SandboxPolicySpec,
    limits: SandboxLimits,
    workspace_host: str,
    tenant_id: str,
    run_id: str,
    container_name: str,
    command: tuple[str, ...],
    env: dict[str, str],
    runtime: str | None = None,
    user: str = DEFAULT_CONTAINER_USER,
) -> list[str]:
    """Build the exact ``docker run`` argv for one sandboxed execution.

    Pure and deterministic (env pairs are sorted) so tests can assert
    every hard limit. Raises :class:`PolicyDenied` for egress terms
    this backend cannot enforce.
    """
    if user.strip() in ("", "0", "0:0", "root", "root:root"):
        raise PolicyDenied(f"refusing to run a sandbox container as root: {user!r}")
    argv: list[str] = [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--label",
        "forgeflow.sandbox=true",
        "--label",
        f"forgeflow.tenant_id={tenant_id}",
        "--label",
        f"forgeflow.run_id={run_id}",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        user,
        "--memory",
        str(limits.memory_bytes),
        "--memory-swap",
        str(limits.memory_bytes),
        "--cpus",
        str(limits.cpu_cores),
        "--pids-limit",
        str(limits.pids_limit),
        "--ulimit",
        f"fsize={limits.file_size_bytes}:{limits.file_size_bytes}",
        "--tmpfs",
        f"/tmp:rw,noexec,nosuid,nodev,size={limits.tmpfs_bytes}",
    ]
    if runtime:
        argv += ["--runtime", runtime]
    # --- network: fail closed unless the terms are exactly expressible --- #
    allowlist = tuple(spec.network_allowlist)
    if not allowlist:
        argv += ["--network", "none"]
    elif allowlist == (OPEN_NETWORK,) and spec.level is SandboxPolicy.FULL_ACCESS:
        argv += ["--network", "bridge"]
    else:
        raise PolicyDenied(
            "the docker sandbox backend cannot enforce a hostname egress "
            f"allowlist {allowlist!r} (docker has no egress filtering); refusing "
            "rather than running with silently widened network access — use an "
            "empty allowlist (no network) or FULL_ACCESS with ('*',) for open egress"
        )
    # --- workspace mount: the only writable area ------------------------- #
    # Docker's --mount CSV grammar accepts key=value fields only: a
    # read-only bind is spelled ``readonly=true`` and a WRITABLE bind
    # carries no marker at all — a bare ``rw``/``ro`` field is rejected
    # by the CLI ("invalid field 'rw' must be a key=value pair").
    readonly_marker = ",readonly=true" if spec.level is SandboxPolicy.READ_ONLY else ""
    argv += [
        "--mount",
        f"type=bind,source={workspace_host},target={_WORKSPACE_TARGET}{readonly_marker}",
        "--workdir",
        _WORKSPACE_TARGET,
    ]
    for mount in spec.extra_ro_mounts:
        argv += ["--mount", f"type=bind,source={mount},target={mount},readonly=true"]
    for key in sorted(env):
        argv += ["--env", f"{key}={env[key]}"]
    argv.append(image)
    argv += list(command)
    return argv


class DockerBackend(BaseSandboxRuntime):
    """One ephemeral container per ``execute()`` (``--rm``).

    With ``runtime="runsc"`` this is the gVisor backend; availability
    then requires runsc to be registered with the daemon.
    """

    name = "docker"

    def __init__(
        self,
        *,
        image: str = "python:3.12-alpine",
        runtime: str | None = None,
        workspace_manager: WorkspaceManager,
        user: str = DEFAULT_CONTAINER_USER,
        docker_binary: str = "docker",
    ) -> None:
        super().__init__(workspace_manager)
        self._image = image
        self._runtime = runtime
        self._user = user
        self._docker = docker_binary
        self.name = "docker-gvisor" if runtime else "docker"
        self._probe_cache: tuple[float, bool] | None = None

    @property
    def image(self) -> str:
        return self._image

    # -- availability ------------------------------------------------------ #
    def available(self) -> bool:
        """Daemon reachable (+ runsc registered, for the gVisor flavour).

        Cached for 30s so a hot tool path does not probe the daemon on
        every call; a stale "available" still fails closed at execute
        time because ``docker run`` itself errors before running code.
        """
        now = time.monotonic()
        if self._probe_cache is not None and now - self._probe_cache[0] < 30.0:
            return self._probe_cache[1]
        ok = self._probe()
        self._probe_cache = (now, ok)
        return ok

    def _probe(self) -> bool:
        from forgeflow.sandbox import docker_isolated

        if docker_isolated.docker_version() is None:
            return False
        if self._runtime:
            return self._runtime_registered(self._runtime)
        return True

    def _runtime_registered(self, runtime: str) -> bool:
        try:
            proc = subprocess.run(  # noqa: S603 — fixed argv, local docker CLI
                [self._docker, "info", "--format", "{{json .Runtimes}}"],
                capture_output=True,
                text=True,
                timeout=8,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return proc.returncode == 0 and f'"{runtime}"' in (proc.stdout or "")

    # -- execution ---------------------------------------------------------- #
    def _code_argv(self, workspace: SandboxWorkspace, filename: str) -> tuple[str, ...]:
        return ("python", f"{_WORKSPACE_TARGET}/{filename}")

    def _execute_in_workspace(
        self,
        request: SandboxRequest,
        workspace: SandboxWorkspace,
        argv: tuple[str, ...],
        cwd: Path,
    ) -> SandboxResult:
        env = container_env(request.env)
        container_name = f"ffsbx-{workspace.execution_id}"
        rel_cwd = cwd.resolve().relative_to(workspace.root.resolve())
        command = list(argv)
        docker_argv = build_docker_argv(
            image=self._image,
            spec=request.policy,
            limits=request.limits,
            workspace_host=str(workspace.root),
            tenant_id=workspace.tenant_id,
            run_id=workspace.run_id,
            container_name=container_name,
            command=tuple(command),
            env=env,
            runtime=self._runtime,
            user=self._user,
        )
        if rel_cwd != Path("."):
            # Re-target the workdir inside the container at the
            # (already workspace-validated) subdirectory.
            idx = docker_argv.index("--workdir")
            docker_argv[idx + 1] = f"{_WORKSPACE_TARGET}/{rel_cwd.as_posix()}"
        proc = subprocess.Popen(  # noqa: S603 — argv built by build_docker_argv
            docker_argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        timed_out = False
        try:
            stdout, stderr = proc.communicate(request.stdin, timeout=request.limits.timeout_s + 5.0)
            exit_code: int | None = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            # The container IS the process group: killing it kills the
            # whole tree deterministically (no host-side group math).
            subprocess.run(  # noqa: S603
                [self._docker, "kill", container_name],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.poll() is None:
                proc.kill()
            stdout, stderr = proc.communicate()
            exit_code = None
        finally:
            # Belt-and-braces residue sweep (--rm only fires on exit).
            subprocess.run(  # noqa: S603
                [self._docker, "rm", "-f", container_name],
                capture_output=True,
                text=True,
                timeout=10,
            )
        # Docker reports an OOM-killed container as exit 137 (128+SIGKILL);
        # with --rm the container is gone, so inspect-based confirmation is
        # unavailable — the same heuristic the legacy backend used.
        oom_killed = bool(not timed_out and exit_code == 137)
        return SandboxResult(
            stdout=stdout or "",
            stderr=stderr or "",
            exit_code=exit_code,
            timed_out=timed_out,
            oom_killed=oom_killed,
            quota_exceeded="memory" if oom_killed else None,
        )

    # -- maintenance --------------------------------------------------------- #
    def cleanup_labeled(self, *, run_id: str | None = None, tenant_id: str | None = None) -> int:
        """Force-remove leftover sandbox containers (by label).

        The reaper for containers whose ``--rm`` never fired (host
        crash, daemon restart). Returns how many were removed. Raises
        nothing: a failed sweep reports 0 and the residue stays
        visible to the next sweep.
        """
        filters = ["--filter", "label=forgeflow.sandbox=true"]
        if run_id:
            filters += ["--filter", f"label=forgeflow.run_id={run_id}"]
        if tenant_id:
            filters += ["--filter", f"label=forgeflow.tenant_id={tenant_id}"]
        try:
            listed = subprocess.run(  # noqa: S603
                [self._docker, "ps", "-aq", *filters],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            return 0
        removed = 0
        for container_id in (listed.stdout or "").split():
            try:
                done = subprocess.run(  # noqa: S603
                    [self._docker, "rm", "-f", container_id],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if done.returncode == 0:
                removed += 1
        return removed
