"""Phase 3 — the unified tool-execution sandbox runtime.

Why this module
---------------
Before this phase the repository carried **two** one-shot sandboxes plus a
third execution substrate, none of them behind a shared contract:

* :mod:`forgeflow.skills.sandbox_isolated` — the *native* runner (independent
  subprocess + interpreter-level network/write guards + a supervising quota
  enforcer; kernel quotas only on Windows via Job Objects, explicitly
  ``degraded``/``unlanded`` elsewhere);
* :mod:`forgeflow.sandbox.docker_isolated` — the *container* runner (Docker),
  which enforced ``--network none`` / ``--read-only`` / ``--memory`` +
  ``--memory-swap`` / ``--cpus`` / ``--pids-limit`` / ``--ulimit fsize`` and an
  env whitelist, but **not** ``--cap-drop ALL``, ``no-new-privileges`` or a
  non-root ``--user`` (containers ran as the image default — root for
  ``python:3.12-alpine``), had no workspace bind mount (source arrived on
  stdin), no tenant/run labels, and no gVisor option;
* the code plane (:mod:`forgeflow.codeplane`) — the *agentic* substrate behind
  the ``code.execute`` tool (multi-turn OpenHands runner over a stateful,
  git-backed workspace with a model-gateway network). It is **not** a one-shot
  executor and is not replaced by this module; it is governed by the same
  policy vocabulary through the executor gate (see
  :mod:`forgeflow.runtime.tool_executor`).

This module is the **one** contract for one-shot code/command execution
(OpenHands Runtime / codex ``SandboxPolicy`` / E2B shape):

* :class:`SandboxPolicy` — ``READ_ONLY`` / ``WORKSPACE_WRITE`` /
  ``FULL_ACCESS`` (codex-style levels), carried by :class:`SandboxPolicySpec`
  together with the network egress allowlist and extra read-only mounts;
* :class:`SandboxLimits` — CPU / memory / pids / disk / file-size / tmpfs /
  wall-clock / output budgets, with the defaults in exactly one place;
* :class:`SandboxRuntime` — the pluggable backend protocol
  (``execute(request) -> SandboxResult``) with four backends:
  :class:`~forgeflow.sandbox.docker_backend.DockerBackend` (default),
  the same backend with the gVisor ``runsc`` runtime (config-enabled),
  :class:`RemoteBackend` (interface-reserved E2B-like stub — fail closed),
  and :class:`LocalProcessBackend` (dev/test only, hard-gated);
* :class:`WorkspaceManager` — per-tenant / per-run workspaces with
  traversal- and symlink-safe path resolution.

Fail-closed discipline (mirrors the run-lease phase):

* an unavailable backend raises :class:`SandboxUnavailable` **before**
  anything executes — there is never a silent fallback to a weaker
  substrate;
* an illegal backend for the environment raises
  :class:`SandboxConfigurationError` (the analogue of
  ``RunLeaseConfigurationError``);
* a policy the backend cannot enforce (e.g. a hostname egress allowlist on
  plain Docker, which has no egress filtering) raises :class:`PolicyDenied`
  instead of silently running with open network;
* the host environment is never inherited wholesale — containers and local
  children receive a small allowlisted base environment plus explicitly
  requested variables, and credential-shaped variable names are refused.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "ALLOW_LOCAL_ENV",
    "DEFAULT_LIMITS",
    "DEFAULT_POLICY",
    "OPEN_NETWORK",
    "LocalProcessBackend",
    "PolicyDenied",
    "RemoteBackend",
    "SandboxConfigurationError",
    "SandboxError",
    "SandboxLimits",
    "SandboxPolicy",
    "SandboxPolicySpec",
    "SandboxRequest",
    "SandboxResult",
    "SandboxRuntime",
    "SandboxUnavailable",
    "SandboxWorkspace",
    "WorkspaceManager",
    "build_sandbox_runtime",
    "get_sandbox_runtime",
    "local_sandbox_allowed",
    "reset_sandbox_runtime",
    "sanitize_path_component",
    "validate_requested_env",
]


# --------------------------------------------------------------------------- #
# Errors                                                                        #
# --------------------------------------------------------------------------- #
class SandboxError(RuntimeError):
    """Base class for every sandbox-runtime failure."""


class SandboxUnavailable(SandboxError):
    """The selected backend cannot run here (daemon down, runtime missing,
    remote backend not implemented). Raised **before** anything executes —
    the fail-closed signal callers must not paper over with a fallback."""


class SandboxConfigurationError(SandboxError):
    """The configured backend is illegal for this environment (e.g. the
    local process backend outside the explicit dev/test opt-in). Mirrors
    ``forgeflow.runtime.run_lease.RunLeaseConfigurationError``."""


class PolicyDenied(SandboxError):
    """The request violates the sandbox policy (path escape, unenforceable
    egress allowlist, credential-shaped env, escalation beyond a tool's
    declared maximum…). Nothing was executed."""


# --------------------------------------------------------------------------- #
# Policy (codex-style)                                                          #
# --------------------------------------------------------------------------- #
class SandboxPolicy(Enum):
    """The codex-style sandbox policy levels, least → most privileged."""

    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    FULL_ACCESS = "full_access"

    @property
    def rank(self) -> int:
        """Numeric privilege (higher = more privileged)."""
        return _POLICY_RANK[self]

    @classmethod
    def from_value(cls, raw: Any) -> SandboxPolicy | None:
        """Parse a policy from its string value (or pass an instance
        through); ``None`` for anything unrecognised — callers fail closed
        on ``None`` rather than guessing a level."""
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, str):
            try:
                return cls(raw.strip().lower())
            except ValueError:
                return None
        return None


_POLICY_RANK: dict[SandboxPolicy, int] = {
    SandboxPolicy.READ_ONLY: 0,
    SandboxPolicy.WORKSPACE_WRITE: 1,
    SandboxPolicy.FULL_ACCESS: 2,
}

#: Allowlist marker meaning "open network" (only ever honoured at
#: ``FULL_ACCESS`` on a backend that can express it — see the docker
#: backend's network rules).
OPEN_NETWORK = "*"


@dataclass(frozen=True)
class SandboxPolicySpec:
    """One execution's full policy: the level plus its network/mount terms.

    * ``level`` — a :class:`SandboxPolicy` (default ``READ_ONLY``).
    * ``network_allowlist`` — egress hostnames the execution may reach.
      Empty (the default) means **no network at all**. A backend that
      cannot enforce hostname-level egress must refuse a non-empty
      allowlist (:class:`PolicyDenied`) — it may never silently widen it
      to open network. ``("*",)`` requests open network and is honoured
      only at ``FULL_ACCESS``.
    * ``extra_ro_mounts`` — additional host paths mounted read-only
      (platform-supplied, never model-supplied; each is validated to
      exist before the backend runs).
    """

    level: SandboxPolicy = SandboxPolicy.READ_ONLY
    network_allowlist: tuple[str, ...] = ()
    extra_ro_mounts: tuple[str, ...] = ()

    @classmethod
    def read_only(cls) -> SandboxPolicySpec:
        return cls(level=SandboxPolicy.READ_ONLY)

    @classmethod
    def workspace_write(cls) -> SandboxPolicySpec:
        return cls(level=SandboxPolicy.WORKSPACE_WRITE)

    @classmethod
    def full_access(cls, *, open_network: bool = False) -> SandboxPolicySpec:
        return cls(
            level=SandboxPolicy.FULL_ACCESS,
            network_allowlist=(OPEN_NETWORK,) if open_network else (),
        )


#: The platform default policy: read-only, no network.
DEFAULT_POLICY = SandboxPolicySpec()


# --------------------------------------------------------------------------- #
# Limits                                                                        #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SandboxLimits:
    """The hard budget one execution must stay inside (backend-enforced).

    * ``cpu_cores`` — CPU quota (docker ``--cpus``; local ``RLIMIT_CPU``
      derives from the wall clock instead — see the local backend).
    * ``memory_bytes`` — memory ceiling (docker ``--memory`` **and**
      ``--memory-swap`` set to the same value: no swap headroom).
    * ``pids_limit`` — process-count ceiling (docker ``--pids-limit``;
      not expressible per-process on the local backend — documented
      there).
    * ``disk_bytes`` — workspace quota: the ``/tmp`` tmpfs cap and the
      post-execution workspace measurement; a bind-mounted workspace
      cannot be hard-capped by Docker without a storage-driver quota,
      so the runtime measures and reports (``quota_exceeded="disk"``).
    * ``file_size_bytes`` — single-file ceiling (docker ``--ulimit
      fsize``; local ``RLIMIT_FSIZE``).
    * ``tmpfs_bytes`` — size cap for the container ``/tmp`` tmpfs.
    * ``timeout_s`` — wall clock; on expiry the whole container /
      process group is killed.
    * ``max_output_chars`` — stdout/stderr truncation cap (the cap
      includes the truncation marker, mirroring
      ``forgeflow.runtime.retry_policy.truncate_feedback``).
    """

    cpu_cores: float = 1.0
    memory_bytes: int = 512 * 1024 * 1024
    pids_limit: int = 64
    disk_bytes: int = 256 * 1024 * 1024
    file_size_bytes: int = 32 * 1024 * 1024
    tmpfs_bytes: int = 64 * 1024 * 1024
    timeout_s: float = 30.0
    max_output_chars: int = 1_000_000


#: The platform default limits.
DEFAULT_LIMITS = SandboxLimits()


# --------------------------------------------------------------------------- #
# Request / result                                                              #
# --------------------------------------------------------------------------- #
@dataclass
class SandboxRequest:
    """One one-shot execution.

    Exactly one of ``argv`` / ``code`` must be given. ``code`` is a
    source payload in ``language`` (only ``"python"`` today — the
    substrate the repository's sandboxes have always run); the backend
    writes it into the workspace and executes it there. ``cwd`` is a
    path **relative to the workspace** (absolute paths and escapes are
    rejected by the workspace resolver before any backend runs).
    """

    argv: tuple[str, ...] | None = None
    code: str | None = None
    language: str = "python"
    policy: SandboxPolicySpec = field(default_factory=SandboxPolicySpec)
    limits: SandboxLimits = field(default_factory=SandboxLimits)
    tenant_id: str = ""
    run_id: str = ""
    env: dict[str, str] = field(default_factory=dict)
    stdin: str | None = None
    cwd: str = ""
    keep_workspace: bool = False


@dataclass
class SandboxResult:
    """The truthful outcome of one execution.

    ``exit_code`` is ``None`` when the execution was killed (timeout /
    watchdog) rather than exiting. ``oom_killed`` is the backend's
    kernel-evidence verdict (docker exit 137 / local SIGKILL heuristic —
    see the backends). A killed or quota-tripped execution is data,
    never an exception: only *refusals to run at all* raise.
    """

    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    oom_killed: bool = False
    duration_s: float = 0.0
    backend: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    quota_exceeded: str | None = None  # "timeout" | "memory" | "disk" | None
    workspace_path: str = ""
    workspace_removed: bool = False


# --------------------------------------------------------------------------- #
# Environment discipline                                                        #
# --------------------------------------------------------------------------- #
#: Base environment for containers: fixed, minimal, host-independent.
_CONTAINER_BASE_ENV: dict[str, str] = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
    "HOME": "/tmp",
}

#: Host variables a *local* child may inherit (mirrors the native
#: sandbox's whitelist idea — the parent's environment is never copied).
_LOCAL_BASE_ENV_KEYS: tuple[str, ...] = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "LANG",
    "LC_ALL",
    "NUMBER_OF_PROCESSORS",
)

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Credential-shaped variable names are refused when *requested*:
#: a sandbox execution must never become a channel that carries a
#: production secret into untrusted code. Matching is deliberately
#: broad (fail-closed): a refused name can be renamed by the caller.
_CREDENTIAL_ENV_RE = re.compile(
    r"(SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE_KEY|CREDENTIAL|API_KEY|ACCESS_KEY"
    r"|DATABASE_URL|POSTGRES_URL|DSN)$"
    r"|^(AWS_|GCP_|AZURE_|OPENAI_|ANTHROPIC_|TAVILY_)",
)


def validate_requested_env(env: dict[str, str] | None) -> dict[str, str]:
    """Validate caller-requested environment variables for an execution.

    Returns a plain ``{str: str}`` copy. Raises :class:`PolicyDenied`
    for malformed names and for credential-shaped names — silently
    dropping a variable would hide the caller's mistake, and passing it
    through would leak a secret into untrusted code.
    """
    out: dict[str, str] = {}
    for key, value in (env or {}).items():
        name = str(key)
        if not _ENV_NAME_RE.match(name):
            raise PolicyDenied(f"sandbox env name is not a valid identifier: {name!r}")
        if _CREDENTIAL_ENV_RE.search(name):
            raise PolicyDenied(
                f"sandbox env name looks like a credential and is refused: {name!r} "
                "(sandboxed code never receives production secrets)"
            )
        out[name] = str(value)
    return out


def container_env(requested: dict[str, str] | None) -> dict[str, str]:
    """The environment a container runs with: fixed base + requested."""
    env = dict(_CONTAINER_BASE_ENV)
    env.update(validate_requested_env(requested))
    return env


def local_env(requested: dict[str, str] | None) -> dict[str, str]:
    """The environment a local child runs with: whitelisted host base
    + requested. ``os.environ`` is never copied wholesale."""
    env = {key: os.environ[key] for key in _LOCAL_BASE_ENV_KEYS if key in os.environ}
    env.update(validate_requested_env(requested))
    return env


# --------------------------------------------------------------------------- #
# Workspace management + path safety                                            #
# --------------------------------------------------------------------------- #
_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


def sanitize_path_component(raw: Any, *, what: str) -> str:
    """Validate one tenant/run id as a single safe path component.

    Rejects (with :class:`PolicyDenied`) anything empty, a dot segment,
    or containing a separator / NUL / leading dot — an id must never be
    able to walk out of the sandbox root or smuggle a nested path.
    """
    value = str(raw or "")
    if (
        not value
        or value in {".", ".."}
        or value.startswith(".")
        or "/" in value
        or "\\" in value
        or "\x00" in value
        or not _COMPONENT_RE.match(value)
    ):
        raise PolicyDenied(f"unsafe sandbox {what} id: {value!r}")
    return value


@dataclass
class SandboxWorkspace:
    """A prepared per-execution workspace directory on the host."""

    root: Path
    tenant_id: str
    run_id: str
    execution_id: str

    #: Fixed in-sandbox mount point (containers) / logical root.
    mount_target: str = "/workspace"


class WorkspaceManager:
    """Creates, resolves-within, measures and destroys workspaces.

    Layout: ``<root>/<tenant_id>/<run_id>/<execution_id>`` — two
    tenants' workspaces are disjoint by construction, and a backend
    mounts exactly one execution directory, never a parent.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def prepare(self, tenant_id: str, run_id: str) -> SandboxWorkspace:
        """Create a fresh workspace for one execution."""
        tenant = sanitize_path_component(tenant_id or "default", what="tenant")
        run = sanitize_path_component(run_id or "default", what="run")
        execution_id = uuid.uuid4().hex[:16]
        path = self._root / tenant / run / execution_id
        path.mkdir(parents=True, exist_ok=False)
        return SandboxWorkspace(root=path, tenant_id=tenant, run_id=run, execution_id=execution_id)

    def resolve_within(self, workspace: SandboxWorkspace, candidate: str | Path) -> Path:
        """Resolve ``candidate`` against the workspace, refusing escapes.

        Relative candidates are taken relative to the workspace root;
        absolute candidates must already be inside it. Resolution is
        symlink-aware (``Path.resolve``), so a symlink inside the
        workspace that points outside is an escape, not a path.
        """
        base = workspace.root.resolve()
        raw = Path(candidate)
        target = raw.resolve() if raw.is_absolute() else (base / raw).resolve()
        if target != base and base not in target.parents:
            raise PolicyDenied(
                f"path escapes the sandbox workspace: {candidate!r} "
                f"(resolves to {target}, workspace is {base})"
            )
        return target

    def measure_size(self, workspace: SandboxWorkspace) -> int:
        """Total bytes of every file under the workspace (best-effort)."""
        total = 0
        for dirpath, _dirs, files in os.walk(workspace.root):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    pass
        return total

    def cleanup(self, workspace: SandboxWorkspace) -> bool:
        """Destroy the workspace tree. Returns True when it is gone."""
        shutil.rmtree(workspace.root, ignore_errors=True)
        return not workspace.root.exists()


# --------------------------------------------------------------------------- #
# The runtime protocol + shared base                                            #
# --------------------------------------------------------------------------- #
@runtime_checkable
class SandboxRuntime(Protocol):
    """The pluggable one-shot execution contract."""

    name: str

    def available(self) -> bool:
        """Whether this backend can execute in this environment now."""
        ...

    def execute(self, request: SandboxRequest) -> SandboxResult:
        """Execute one request, or raise SandboxUnavailable /
        SandboxConfigurationError / PolicyDenied **before** running
        anything. A completed-but-failed execution is a result, not an
        exception."""
        ...


#: File name the code payload is written to inside the workspace.
_CODE_FILENAME = "_sandbox_main.py"


def _truncate_output(text: str, cap: int) -> tuple[str, bool]:
    """Truncate one output stream to ``cap`` chars, marker included —
    the codebase's established truncation discipline."""
    from forgeflow.runtime.retry_policy import truncate_feedback

    return truncate_feedback(text, cap)


class BaseSandboxRuntime:
    """Shared request/workspace/finalisation logic for real backends.

    Subclasses implement :meth:`available` and
    :meth:`_execute_in_workspace`. The base owns: request validation,
    env validation, workspace preparation, code-payload staging, cwd
    resolution, output truncation, the workspace disk-quota
    measurement, and workspace destruction (unless the request asks to
    keep it).
    """

    name: str = "base"

    def __init__(self, workspace_manager: WorkspaceManager) -> None:
        self._workspaces = workspace_manager

    @property
    def workspace_manager(self) -> WorkspaceManager:
        return self._workspaces

    def available(self) -> bool:  # pragma: no cover — overridden
        raise NotImplementedError

    # -- backend hooks ---------------------------------------------------- #
    def _code_argv(self, workspace: SandboxWorkspace, filename: str) -> tuple[str, ...]:
        """Argv that runs a staged code file on this backend."""
        raise NotImplementedError

    def _execute_in_workspace(
        self,
        request: SandboxRequest,
        workspace: SandboxWorkspace,
        argv: tuple[str, ...],
        cwd: Path,
    ) -> SandboxResult:
        raise NotImplementedError

    # -- the template ------------------------------------------------------ #
    def execute(self, request: SandboxRequest) -> SandboxResult:
        if not self.available():
            raise SandboxUnavailable(
                f"sandbox backend {self.name!r} is unavailable in this environment; "
                "refusing to execute (no fallback to a weaker substrate)"
            )
        argv = self._validate_request(request)
        validate_requested_env(request.env)
        for mount in request.policy.extra_ro_mounts:
            if not Path(mount).is_dir():
                raise PolicyDenied(f"extra read-only mount does not exist: {mount!r}")
        workspace = self._workspaces.prepare(request.tenant_id, request.run_id)
        started = time.monotonic()
        try:
            if request.code is not None:
                code_path = workspace.root / _CODE_FILENAME
                code_path.write_text(request.code, encoding="utf-8")
                argv = self._code_argv(workspace, _CODE_FILENAME)
            cwd = (
                self._workspaces.resolve_within(workspace, request.cwd)
                if request.cwd
                else workspace.root
            )
            # The workspace is created fresh for this execution, so a
            # requested subdirectory can only exist if the runtime
            # creates it (it was just proven to resolve *within* the
            # workspace, so creating it cannot escape).
            cwd.mkdir(parents=True, exist_ok=True)
            result = self._execute_in_workspace(request, workspace, tuple(argv), cwd)
        except BaseException:
            # A refused/failed execution leaves no workspace behind
            # (unless the caller explicitly asked to keep it).
            if not request.keep_workspace:
                self._workspaces.cleanup(workspace)
            raise
        result.backend = self.name
        result.duration_s = time.monotonic() - started
        result.workspace_path = str(workspace.root)
        result.stdout, result.stdout_truncated = _truncate_output(
            result.stdout, request.limits.max_output_chars
        )
        result.stderr, result.stderr_truncated = _truncate_output(
            result.stderr, request.limits.max_output_chars
        )
        if result.timed_out and result.quota_exceeded is None:
            result.quota_exceeded = "timeout"
        if (
            result.quota_exceeded is None
            and self._workspaces.measure_size(workspace) > request.limits.disk_bytes
        ):
            result.quota_exceeded = "disk"
        if request.keep_workspace:
            result.workspace_removed = False
        else:
            result.workspace_removed = self._workspaces.cleanup(workspace)
        return result

    def _validate_request(self, request: SandboxRequest) -> tuple[str, ...]:
        if (request.argv is None) == (request.code is None):
            raise PolicyDenied("a sandbox request needs exactly one of argv / code")
        if request.code is not None and request.language != "python":
            raise PolicyDenied(
                f"unsupported sandbox code language: {request.language!r} "
                "(only 'python' is supported)"
            )
        if request.argv is not None:
            if not request.argv or any(not str(a) for a in request.argv):
                raise PolicyDenied("a sandbox argv must be a non-empty command")
            return tuple(str(a) for a in request.argv)
        return ()


# --------------------------------------------------------------------------- #
# Local process backend (dev/test only, hard-gated)                             #
# --------------------------------------------------------------------------- #
#: Explicit opt-in for the local process backend. Only the exact value
#: ``"1"`` grants it — mirroring the run-lease discipline, the mode is
#: never inferred from the environment label alone.
ALLOW_LOCAL_ENV = "FORGEFLOW_SANDBOX_ALLOW_LOCAL"


def local_sandbox_allowed(settings: Any = None) -> bool:
    """Whether the local process backend may be constructed here.

    ALL of the following, mirroring the run-lease gate's discipline:

    * the operator explicitly opted in with
      ``FORGEFLOW_SANDBOX_ALLOW_LOCAL=1`` (a dedicated flag — never
      inferred);
    * the deployment environment is ``dev``
      (``Settings.environment()`` — its fail-closed normalisation means
      a typo'd ``APP_ENV`` reads as ``prod`` and refuses); and
    * the run-lease single-process dev claim holds
      (``FORGEFLOW_SINGLE_PROCESS_DEV=1`` with exactly one configured
      worker): a multi-process deployment may not pretend a local
      subprocess substrate is an isolation boundary shared by peers.

    In every other configuration the factory raises
    :class:`SandboxConfigurationError` — production refuses, always.
    """
    if os.environ.get(ALLOW_LOCAL_ENV) != "1":
        return False
    try:
        if settings is None:
            from forgeflow.config import get_settings

            settings = get_settings()
        environment = str(settings.environment())
    except Exception:  # settings unavailable → fail closed
        return False
    if environment != "dev":
        return False
    from forgeflow.runtime.run_lease import single_process_dev_mode

    return single_process_dev_mode()


def _local_preexec(limits: SandboxLimits) -> Any:
    """Build the ``preexec_fn`` applying POSIX resource limits.

    ``RLIMIT_CPU`` (wall-clock-derived hard kill), ``RLIMIT_AS``
    (address space — set to twice the declared memory with a floor,
    because an interpreter's virtual reservations exceed its resident
    budget; the honest hard memory boundary is the container backend),
    ``RLIMIT_FSIZE`` (single-file ceiling) and ``RLIMIT_NOFILE``.
    ``RLIMIT_NPROC`` is deliberately **not** used: it counts per real
    uid across the whole host, so a small pids budget would make the
    child fail forks the host already spent — the pids limit is a
    container-backend guarantee, not a local one.
    """
    import resource

    cpu_seconds = max(1, int(limits.timeout_s) + 1)
    address_space = max(limits.memory_bytes * 2, 512 * 1024 * 1024)
    file_size = limits.file_size_bytes

    def _apply() -> None:  # pragma: no cover — runs in the forked child
        for res, soft in (
            (resource.RLIMIT_CPU, cpu_seconds),
            (resource.RLIMIT_AS, address_space),
            (resource.RLIMIT_FSIZE, file_size),
            (resource.RLIMIT_NOFILE, 1024),
            (resource.RLIMIT_CORE, 0),
        ):
            try:
                _soft, hard = resource.getrlimit(res)
                new_soft = soft if hard == resource.RLIM_INFINITY else min(soft, hard)
                resource.setrlimit(res, (new_soft, hard))
            except (ValueError, OSError):
                pass

    return _apply


class LocalProcessBackend(BaseSandboxRuntime):
    """Run the command as a local subprocess — **dev/test only**.

    Substrate: own process group (``start_new_session``), the whole
    group is SIGKILLed on timeout, POSIX rlimits via ``preexec_fn``
    where the platform has them, the allowlisted local env, and — for
    Python *code* payloads — the native sandbox's interpreter guards
    (network egress + write-outside-workspace) from
    :mod:`forgeflow.skills.sandbox_isolated`, so a code payload is
    confined to its workspace and (with an empty allowlist) has no
    network. Plain ``argv`` executions get **no** interpreter guards
    (there is no interpreter to guard) and no filesystem confinement
    beyond the working directory: that honesty is exactly why this
    backend is gated to explicit dev/test opt-in and refused in
    production. Kernel-grade isolation is the docker backend's job.
    """

    name = "local"

    def available(self) -> bool:
        return local_sandbox_allowed()

    def _code_argv(self, workspace: SandboxWorkspace, filename: str) -> tuple[str, ...]:
        return (sys.executable, str(workspace.root / filename))

    def _guarded_code_argv(self, workspace: SandboxWorkspace) -> tuple[str, ...]:
        """Argv running the code payload under the native guards.

        The payload path is absolute: the child's cwd may be a
        workspace subdirectory (``SandboxRequest.cwd``), while the
        guard root stays the workspace itself.
        """
        return (
            sys.executable,
            str(workspace.root / "__bootstrap__.py"),
            str(workspace.root / _CODE_FILENAME),
        )

    def _execute_in_workspace(
        self,
        request: SandboxRequest,
        workspace: SandboxWorkspace,
        argv: tuple[str, ...],
        cwd: Path,
    ) -> SandboxResult:
        if not local_sandbox_allowed():
            raise SandboxConfigurationError(
                "the local sandbox backend requires the explicit dev/test opt-in "
                f"({ALLOW_LOCAL_ENV}=1, dev environment, single-process dev mode); "
                "it is refused in every other configuration"
            )
        env = local_env(request.env)
        run_argv: tuple[str, ...] = argv
        if request.code is not None:
            # Converge on the native sandbox's guard bootstrap: the
            # payload runs with socket egress blocked (empty allowlist)
            # and writes confined to the workspace, exactly the
            # semantics forgeflow.skills.sandbox_isolated enforces.
            from forgeflow.skills.sandbox_isolated import _BOOTSTRAP_SRC

            (workspace.root / "__bootstrap__.py").write_text(_BOOTSTRAP_SRC, encoding="utf-8")
            env["FF_SANDBOX_WORKDIR"] = str(workspace.root)
            env["FF_SANDBOX_NETWORK_BLOCK"] = "1" if not request.policy.network_allowlist else "0"
            env["FF_SANDBOX_WRITE_GUARD"] = "1"
            run_argv = self._guarded_code_argv(workspace)
        popen_kwargs: dict[str, Any] = {
            "cwd": str(cwd),
            "env": env,
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
        }
        if os.name == "nt":  # pragma: no cover — Windows process group
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            popen_kwargs["start_new_session"] = True
            popen_kwargs["preexec_fn"] = _local_preexec(request.limits)
        proc = subprocess.Popen(list(run_argv), **popen_kwargs)  # noqa: S603
        timed_out = False
        try:
            stdout, stderr = proc.communicate(request.stdin, timeout=request.limits.timeout_s)
            exit_code: int | None = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill_tree(proc)
            stdout, stderr = proc.communicate()
            exit_code = None
        oom_killed = bool(
            not timed_out and exit_code is not None and exit_code == -getattr(signal, "SIGKILL", 9)
        )
        return SandboxResult(
            stdout=stdout or "",
            stderr=stderr or "",
            exit_code=exit_code,
            timed_out=timed_out,
            oom_killed=oom_killed,
            quota_exceeded="memory" if oom_killed else None,
        )

    @staticmethod
    def _kill_tree(proc: subprocess.Popen[str]) -> None:
        """Kill the child's whole process group (POSIX) / the child and
        its tree (Windows best-effort)."""
        if os.name == "nt":  # pragma: no cover — Windows
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
            if proc.poll() is None:
                proc.kill()
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            if proc.poll() is None:
                proc.kill()


# --------------------------------------------------------------------------- #
# Remote backend (interface-reserved, E2B-like)                                 #
# --------------------------------------------------------------------------- #
class RemoteBackend:
    """Reserved remote sandbox (E2B-like) — **not implemented**.

    Construction and configuration are accepted so deployments can
    select the backend by name, but every execution raises
    :class:`SandboxUnavailable`: a reserved interface must fail closed,
    never silently fall back to a local substrate.
    """

    name = "remote"

    def __init__(self, *, endpoint: str = "", api_key: str = "") -> None:
        self._endpoint = endpoint
        self._api_key = api_key

    def available(self) -> bool:
        return False

    def execute(self, request: SandboxRequest) -> SandboxResult:
        raise SandboxUnavailable(
            "the remote sandbox backend is reserved (E2B-like) but not implemented "
            "in this deployment; refusing to execute rather than falling back to a "
            "local substrate"
        )


# --------------------------------------------------------------------------- #
# Factory (mirrors get_run_lease_store's registry discipline)                   #
# --------------------------------------------------------------------------- #
_SANDBOX_RUNTIME: SandboxRuntime | None = None


def sandbox_root_path(settings: Any = None) -> Path:
    """The root under which per-execution workspaces are created."""
    try:
        if settings is None:
            from forgeflow.config import get_settings

            settings = get_settings()
        configured = str(getattr(settings, "sandbox_root", "") or "").strip()
        if configured:
            return Path(configured)
        return Path(settings.resource_store_path()) / "sandbox"
    except Exception:  # settings unavailable → temp fallback
        import tempfile

        return Path(tempfile.gettempdir()) / "forgeflow" / "sandbox"


def build_sandbox_runtime(settings: Any = None) -> SandboxRuntime:
    """Build the runtime selected by ``Settings.sandbox_backend``.

    ``docker`` (default) / ``docker-gvisor`` / ``remote`` / ``local``.
    The local backend is constructed **only** when
    :func:`local_sandbox_allowed` holds — otherwise, and for any
    unknown backend name, :class:`SandboxConfigurationError` is raised
    (fail closed at resolution time, not at first use).
    """
    if settings is None:
        from forgeflow.config import get_settings

        settings = get_settings()
    backend = str(getattr(settings, "sandbox_backend", "docker") or "docker").strip().lower()
    image = str(getattr(settings, "sandbox_image", "python:3.12-alpine") or "python:3.12-alpine")
    workspaces = WorkspaceManager(sandbox_root_path(settings))
    if backend == "docker":
        from forgeflow.sandbox.docker_backend import DockerBackend

        return DockerBackend(image=image, workspace_manager=workspaces)
    if backend in ("docker-gvisor", "gvisor"):
        from forgeflow.sandbox.docker_backend import DockerBackend

        return DockerBackend(image=image, runtime="runsc", workspace_manager=workspaces)
    if backend == "remote":
        return RemoteBackend(endpoint=str(getattr(settings, "sandbox_remote_endpoint", "") or ""))
    if backend == "local":
        if not local_sandbox_allowed(settings):
            raise SandboxConfigurationError(
                "sandbox_backend='local' is refused here: the local process backend "
                f"requires {ALLOW_LOCAL_ENV}=1, a dev environment, and single-process "
                "dev mode (mirroring the run-lease gate); production always refuses"
            )
        return LocalProcessBackend(workspace_manager=workspaces)
    raise SandboxConfigurationError(f"unknown sandbox backend: {backend!r}")


def get_sandbox_runtime(settings: Any = None) -> SandboxRuntime:
    """The process-wide sandbox runtime (built once, then cached).

    Mirrors ``get_run_lease_store``: resolution is the gate — an
    illegal configuration raises here, at startup/resolution, rather
    than degrading at execution time.
    """
    global _SANDBOX_RUNTIME
    if _SANDBOX_RUNTIME is None:
        _SANDBOX_RUNTIME = build_sandbox_runtime(settings)
    return _SANDBOX_RUNTIME


def reset_sandbox_runtime() -> None:
    """Drop the cached runtime (test helper)."""
    global _SANDBOX_RUNTIME
    _SANDBOX_RUNTIME = None
