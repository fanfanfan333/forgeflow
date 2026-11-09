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
import stat
import subprocess
import sys
import threading
import time
import uuid
import weakref
from collections.abc import Callable
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
    * ``disk_bytes`` — workspace quota: the ``/tmp`` tmpfs cap plus a
      **runtime** enforcer — a monitor measures the workspace during
      execution and hard-kills the run the moment it exceeds the
      budget (``quota_exceeded="disk"``, exit code ``None``); the
      post-execution measurement remains as the backstop for runs
      that finish inside one polling interval.
    * ``file_size_bytes`` — single-file ceiling (docker ``--ulimit
      fsize``; local ``RLIMIT_FSIZE``; at ``READ_ONLY`` the local
      backend pins it to 0, so no regular file can receive bytes).
    * ``tmpfs_bytes`` — size cap for the container ``/tmp`` tmpfs.
    * ``timeout_s`` — wall clock; on expiry the whole container /
      process group is killed.
    * ``max_output_chars`` — stdout/stderr truncation cap (the cap
      includes the truncation marker, mirroring
      ``forgeflow.runtime.retry_policy.truncate_feedback``). The
      capture layer also enforces this as a **byte** cap while
      streaming, so the parent never buffers unbounded output.
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


#: Mode of every directory in the trusted workspace chain (sandbox
#: root, tenant, run, execution leaf). ``0o700``: the chain is private
#: to the owning uid — the same uid the container runs as on a
#: non-root host, or the backend's own uid on a root host. Never
#: ``0o777``: host-side world-writability is not an acceptable way to
#: make a bind mount writable (see ``docker_backend.resolve_container_user``).
_WORKSPACE_DIR_MODE = 0o700

#: ``dir_fd``-relative, no-follow directory operations are available
#: on POSIX; elsewhere the chain falls back to lstat-verified creation
#: (same fail-closed verdicts, weaker TOCTOU posture — the graded
#: deployment platform is POSIX).
_SECURE_FD_OPS = os.name == "posix" and hasattr(os, "O_NOFOLLOW") and hasattr(os, "fchmod")


class WorkspaceManager:
    """Creates, resolves-within, measures and destroys workspaces.

    Layout: ``<root>/<tenant_id>/<run_id>/<execution_id>`` — two
    tenants' workspaces are disjoint by construction, and a backend
    mounts exactly one execution directory, never a parent.

    The chain is **trusted**: every level (root, tenant, run, leaf)
    is created and verified level by level without following symlinks
    (``dir_fd``-relative ``O_NOFOLLOW|O_DIRECTORY`` opens on POSIX),
    must be a real directory owned by the effective uid, and is
    pinned to mode ``0o700``. A symlink planted at any level, or a
    foreign-owned component, fails closed with
    :class:`SandboxConfigurationError` — the chain is never
    resolve-and-continued past a hostile component, so an ancestor
    symlink cannot redirect a workspace outside the sandbox root.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    # -- trusted chain creation ------------------------------------------ #
    def _open_or_create_secure(self, parent_fd: int, name: str) -> tuple[int, bool]:
        """Open ``name`` under ``parent_fd`` without following symlinks.

        Returns ``(fd, created)``. The component must be a directory
        owned by the effective uid; its mode is pinned to
        :data:`_WORKSPACE_DIR_MODE`. Symlinks (``ELOOP``), non-directories
        and foreign-owned components raise
        :class:`SandboxConfigurationError`.
        """
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        created = False
        try:
            fd = os.open(name, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            try:
                os.mkdir(name, _WORKSPACE_DIR_MODE, dir_fd=parent_fd)
            except FileExistsError:
                pass  # lost a creation race — fall through and verify the winner
            created = True
            try:
                fd = os.open(name, flags, dir_fd=parent_fd)
            except OSError as exc:
                raise SandboxConfigurationError(
                    f"sandbox workspace component {name!r} could not be opened "
                    f"after creation: {exc}"
                ) from exc
        except OSError as exc:
            # ELOOP ⇒ the component is a symlink; ENOTDIR ⇒ not a directory.
            raise SandboxConfigurationError(
                f"sandbox workspace component {name!r} is not a trusted directory "
                f"(symlink or non-directory planted in the chain?): {exc}"
            ) from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode):
                raise SandboxConfigurationError(
                    f"sandbox workspace component {name!r} is not a directory"
                )
            euid = os.geteuid() if hasattr(os, "geteuid") else info.st_uid
            if info.st_uid != euid:
                raise SandboxConfigurationError(
                    f"sandbox workspace component {name!r} is owned by uid "
                    f"{info.st_uid}, expected the effective uid {euid} — refusing "
                    "to use a foreign-owned directory in the trusted chain"
                )
            if stat.S_IMODE(info.st_mode) != _WORKSPACE_DIR_MODE:
                os.fchmod(fd, _WORKSPACE_DIR_MODE)
        except BaseException:
            os.close(fd)
            raise
        return fd, created

    def _ensure_chain_posix(self, components: list[str]) -> bool:
        """Create/verify ``components`` under the (absolute) root, dir_fd by dir_fd.

        The root itself is the first component: its parent (operator
        territory, outside the trusted chain) is opened plainly; every
        component from the root down goes through
        :meth:`_open_or_create_secure`. Returns whether the **last**
        component (the execution leaf) was freshly created.
        """
        root_abs = Path(os.path.abspath(self._root))
        os.makedirs(root_abs.parent, exist_ok=True)
        parent_fd = os.open(root_abs.parent, os.O_RDONLY | os.O_DIRECTORY)
        leaf_created = False
        try:
            fd = parent_fd
            for name in [root_abs.name, *components]:
                fd, leaf_created = self._open_or_create_secure(fd, name)
                if fd != parent_fd:
                    os.close(parent_fd)
                parent_fd = fd
        finally:
            os.close(parent_fd)
        return leaf_created

    def _ensure_chain_portable(self, leaf: Path) -> None:
        """Non-POSIX fallback: identical verdicts via ``lstat`` checks.

        Each component is verified (never blindly followed): a symlink
        or non-directory at any level fails closed. Lacks the dir_fd
        TOCTOU immunity of the POSIX path — acceptable only where
        ``O_NOFOLLOW``/``dir_fd`` do not exist.
        """
        root_abs = Path(os.path.abspath(self._root))
        leaf_abs = Path(os.path.abspath(leaf))
        parts = leaf_abs.relative_to(root_abs).parts
        levels = [root_abs]
        for i in range(len(parts)):
            levels.append(root_abs.joinpath(*parts[: i + 1]))
        for path in levels:
            try:
                info = path.lstat()
            except FileNotFoundError:
                try:
                    path.mkdir(mode=_WORKSPACE_DIR_MODE)
                except FileExistsError:
                    info = path.lstat()
                else:
                    continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise SandboxConfigurationError(
                    f"sandbox workspace component {str(path)!r} is not a trusted "
                    "directory (symlink or non-directory planted in the chain?)"
                )
            try:
                path.chmod(_WORKSPACE_DIR_MODE)
            except OSError:
                pass  # mode pinning is best-effort off POSIX

    def prepare(self, tenant_id: str, run_id: str) -> SandboxWorkspace:
        """Create a fresh workspace for one execution."""
        tenant = sanitize_path_component(tenant_id or "default", what="tenant")
        run = sanitize_path_component(run_id or "default", what="run")
        for _attempt in range(3):
            execution_id = uuid.uuid4().hex[:16]
            path = self._root / tenant / run / execution_id
            if _SECURE_FD_OPS:
                fresh = self._ensure_chain_posix([tenant, run, execution_id])
            else:
                fresh = not path.exists()
                self._ensure_chain_portable(path)
            if fresh and path.is_dir():
                return SandboxWorkspace(
                    root=path, tenant_id=tenant, run_id=run, execution_id=execution_id
                )
        raise SandboxConfigurationError(
            "could not create a fresh sandbox workspace (execution-id collision?)"
        )

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


# --------------------------------------------------------------------------- #
# Bounded streaming capture + runtime quota monitor                             #
# --------------------------------------------------------------------------- #
class _StreamCapture:
    """One output stream's bounded sink.

    Reader threads feed chunks in; only the first ``cap_bytes`` are
    retained — the parent **never buffers unbounded output**, however
    much the child emits. ``total`` still counts every byte so the
    truncation verdict is exact.
    """

    def __init__(self, cap_bytes: int) -> None:
        self._cap = max(0, cap_bytes)
        self._buf = bytearray()
        self._lock = threading.Lock()
        self.total = 0

    def feed(self, chunk: bytes) -> None:
        with self._lock:
            self.total += len(chunk)
            room = self._cap - len(self._buf)
            if room > 0:
                self._buf += chunk[:room]

    @property
    def truncated(self) -> bool:
        return self.total > self._cap

    def text(self) -> str:
        return bytes(self._buf).decode("utf-8", errors="replace")


def _pump_stream(stream: Any, capture: _StreamCapture) -> None:
    """Drain one pipe into its bounded capture until EOF."""
    fd = stream.fileno()
    while True:
        try:
            chunk = os.read(fd, 65536)
        except (OSError, ValueError):
            return
        if not chunk:
            return
        capture.feed(chunk)


def _feed_stdin(stream: Any, data: str | None) -> None:
    """Write the request's stdin to the child, then close (EOF)."""
    try:
        if data:
            stream.write(data.encode("utf-8", errors="replace"))
            stream.flush()
    except (OSError, ValueError):
        pass  # the child is gone; its exit state carries the story
    try:
        stream.close()
    except (OSError, ValueError):
        pass


@dataclass
class BoundedOutcome:
    """The result of one bounded process run (see :func:`run_bounded`)."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    #: Quota the runtime monitor tripped on ("disk"), if any.
    violation: str | None = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False


#: How often the supervisor loop wakes; the disk monitor is consulted
#: every fourth wake (≈0.2s) — the stated polling slack for the disk
#: enforcer: a run may exceed the budget by at most what it writes
#: inside one interval before the kill lands.
_SUPERVISOR_POLL_S = 0.05
_DISK_CHECK_EVERY = 4


def run_bounded(
    proc: subprocess.Popen[bytes],
    *,
    stdin_data: str | None,
    timeout_s: float,
    cap_bytes: int,
    kill: Callable[[], None],
    monitor: Callable[[], str | None] | None = None,
) -> BoundedOutcome:
    """Supervise ``proc`` to completion with bounded output + quotas.

    * stdout/stderr are drained by reader threads into
      :class:`_StreamCapture` sinks capped at ``cap_bytes`` **each** —
      memory stays bounded no matter how much the child prints;
    * at ``timeout_s`` the run is declared timed out and ``kill()``
      fires (the backend's whole-tree/container kill);
    * ``monitor`` (when given) is polled during the run; the first
      non-``None`` verdict (e.g. ``"disk"``) kills the run the same
      way — this is the **runtime** disk enforcer, as opposed to the
      post-execution measurement backstop;
    * a killed run reports ``exit_code=None``; a run that exits on its
      own reports its real code.
    """
    out_cap = _StreamCapture(cap_bytes)
    err_cap = _StreamCapture(cap_bytes)
    threads = [
        threading.Thread(target=_pump_stream, args=(proc.stdout, out_cap), daemon=True),
        threading.Thread(target=_pump_stream, args=(proc.stderr, err_cap), daemon=True),
        threading.Thread(target=_feed_stdin, args=(proc.stdin, stdin_data), daemon=True),
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + max(0.0, timeout_s)
    timed_out = False
    violation: str | None = None
    iteration = 0
    try:
        while True:
            if proc.poll() is not None:
                break
            now = time.monotonic()
            if now >= deadline:
                timed_out = True
                kill()
                break
            if monitor is not None and iteration % _DISK_CHECK_EVERY == 0:
                verdict = monitor()
                if verdict is not None:
                    violation = verdict
                    kill()
                    break
            iteration += 1
            time.sleep(_SUPERVISOR_POLL_S)
    except BaseException:
        # A supervisor failure must never strand the child.
        try:
            kill()
        except Exception:  # noqa: BLE001 — reaping is best-effort here
            pass
        raise
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            kill()
        except Exception:  # noqa: BLE001
            pass
        proc.wait(timeout=10)
    for thread in threads[:2]:
        thread.join(timeout=5)
    killed = timed_out or violation is not None
    return BoundedOutcome(
        stdout=out_cap.text(),
        stderr=err_cap.text(),
        exit_code=None if killed else proc.returncode,
        timed_out=timed_out,
        violation=violation,
        stdout_truncated=out_cap.truncated,
        stderr_truncated=err_cap.truncated,
    )


def workspace_disk_monitor(
    workspaces: WorkspaceManager, workspace: SandboxWorkspace, disk_bytes: int
) -> Callable[[], str | None]:
    """The runtime disk enforcer for one execution: ``"disk"`` the
    moment the workspace's measured size exceeds the budget."""

    def _check() -> str | None:
        try:
            if workspaces.measure_size(workspace) > disk_bytes:
                return "disk"
        except OSError:
            pass
        return None

    return _check


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
        result.stdout, stdout_cut = _truncate_output(result.stdout, request.limits.max_output_chars)
        result.stdout_truncated = result.stdout_truncated or stdout_cut
        result.stderr, stderr_cut = _truncate_output(result.stderr, request.limits.max_output_chars)
        result.stderr_truncated = result.stderr_truncated or stderr_cut
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


def _local_preexec(limits: SandboxLimits, *, read_only: bool = False) -> Any:
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

    At ``READ_ONLY`` the file-size ceiling is pinned to **0**: no
    regular file can receive a single byte, anywhere on the host —
    the kernel-level half of the local read-only guarantee (the
    interpreter guard gives code payloads the clean denial).
    """
    import resource

    cpu_seconds = max(1, int(limits.timeout_s) + 1)
    address_space = max(limits.memory_bytes * 2, 512 * 1024 * 1024)
    file_size = 0 if read_only else limits.file_size_bytes

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


def _descendant_pids(root_pid: int) -> list[int]:
    """``root_pid`` plus every live descendant (``/proc`` parent chain).

    Implemented here rather than reusing the native sandbox's
    ``descendant_pids``: that helper's POSIX children map is a stub
    (it is a Windows Job-Object-era API), so on Linux it only ever
    returns the root itself — a hollow guarantee for tree reaping.
    The parent chain is read from ``/proc/<pid>/stat`` (the ``comm``
    field may contain spaces/parens, so parsing resumes after the
    last ``)``). Falls back to ``[root_pid]`` when ``/proc`` is
    unreadable.
    """
    children: dict[int, list[int]] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return [root_pid]
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as fh:
                raw = fh.read()
            fields = raw[raw.rfind(b")") + 2 :].split()
            ppid = int(fields[1])  # fields[0] = state, fields[1] = ppid
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(int(name))
    seen = [root_pid]
    seen_set = {root_pid}
    stack = [root_pid]
    while stack:
        current = stack.pop()
        for child in children.get(current, []):
            if child not in seen_set:
                seen_set.add(child)
                seen.append(child)
                stack.append(child)
    return seen


# --------------------------------------------------------------------------- #
# Local backend process discipline (mirrors the run-lease stores)               #
# --------------------------------------------------------------------------- #
#: Live local backends in this process (for the at-fork invalidation).
_LOCAL_BACKEND_INSTANCES: weakref.WeakSet[LocalProcessBackend] = weakref.WeakSet()

#: Per-root flock claims held by this process: sandbox root → lock fd.
#: The local backend is a *single-process* substrate; the claim is the
#: positive proof (an ``flock`` on a file under the root) that no other
#: process is operating the same local backend root.
_LOCAL_ROOT_CLAIMS: dict[str, int] = {}
_LOCAL_CLAIMS_PID = os.getpid()


def _reset_local_backend_state_after_fork() -> None:
    """``os.register_at_fork`` child hook: invalidate inherited state.

    A forked child inherits the backend objects and the claim table by
    copy; both are the parent's. The claims table is dropped (the
    inherited lock fds are deliberately left open — closing one
    duplicate would not release the parent's lock, and the child's own
    claim attempt must contend honestly), and every inherited backend
    instance is stamped with a dead pid so its next use raises
    :class:`SandboxConfigurationError` instead of serving half-valid
    state (the run-lease stores' discipline).
    """
    global _LOCAL_CLAIMS_PID
    _LOCAL_ROOT_CLAIMS.clear()
    _LOCAL_CLAIMS_PID = os.getpid()
    for backend in list(_LOCAL_BACKEND_INSTANCES):
        backend._created_pid = -1


if hasattr(os, "register_at_fork"):  # POSIX; unavailable on some platforms
    os.register_at_fork(after_in_child=_reset_local_backend_state_after_fork)


def _claim_local_root(root: Path) -> None:
    """Claim ``root`` for this process's local backend (flock-held).

    Idempotent within a process. Raises
    :class:`SandboxConfigurationError` when another live process
    holds the claim — two processes must never operate one local
    backend root concurrently.
    """
    global _LOCAL_CLAIMS_PID
    pid = os.getpid()
    if pid != _LOCAL_CLAIMS_PID:  # hook missing / state inherited oddly
        _LOCAL_ROOT_CLAIMS.clear()
        _LOCAL_CLAIMS_PID = pid
    key = str(Path(os.path.abspath(root)))
    if key in _LOCAL_ROOT_CLAIMS:
        return
    os.makedirs(key, exist_ok=True)
    fd = os.open(Path(key) / ".forgeflow-local-backend.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "posix":
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif os.name == "nt":  # pragma: no cover — Windows byte-range lock
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
    except OSError as exc:
        os.close(fd)
        raise SandboxConfigurationError(
            f"the local sandbox root {key!r} is already claimed by another "
            "process: the local backend is a single-process substrate and "
            "refuses to share a root across processes"
        ) from exc
    _LOCAL_ROOT_CLAIMS[key] = fd


_NETNS_PREFIX_UNSET = object()
_netns_prefix_cache: Any = _NETNS_PREFIX_UNSET


def _netns_prefix() -> tuple[str, ...] | None:
    """The ``unshare`` prefix giving a child a private network
    namespace (no interfaces ⇒ no network), or ``None`` when this
    host cannot create one.

    Probed once per process with the unprivileged combo
    (``--user --map-root-user --net``), which works both for root and
    for hosts with unprivileged user namespaces enabled.
    """
    global _netns_prefix_cache
    if _netns_prefix_cache is not _NETNS_PREFIX_UNSET:
        return _netns_prefix_cache
    prefix: tuple[str, ...] | None = None
    exe = shutil.which("unshare")
    if exe and os.name == "posix":
        try:
            done = subprocess.run(
                [exe, "--user", "--map-root-user", "--net", "true"],
                capture_output=True,
                timeout=5,
            )
            if done.returncode == 0:
                prefix = (exe, "--user", "--map-root-user", "--net")
        except (OSError, subprocess.TimeoutExpired):
            prefix = None
    _netns_prefix_cache = prefix
    return prefix


class LocalProcessBackend(BaseSandboxRuntime):
    """Run the command as a local subprocess — **dev/test only**.

    Substrate: own process group (``start_new_session``), POSIX
    rlimits via ``preexec_fn`` where the platform has them, the
    allowlisted local env, bounded streaming output, the runtime
    disk monitor, and whole-**tree** reaping on timeout/error —
    descendants are discovered through ``/proc`` *before* the kill,
    so a grandchild that escaped the process group via ``setsid``
    is killed too, not just the group.

    Policy really takes effect locally:

    * ``READ_ONLY`` — code payloads get the native sandbox's
      interpreter write guard in deny-all mode (``FF_SANDBOX_READ_ONLY``),
      and every payload additionally runs under ``RLIMIT_FSIZE=0``:
      no regular file can receive bytes, in or out of the workspace;
    * network, docker-aligned — an empty allowlist means **no
      network**: code payloads are blocked by the interpreter guard
      and (where the host can create one) a private network
      namespace via ``unshare``; ``argv`` payloads *require* the
      namespace and are refused without it, there being no
      interpreter to guard. A specific-host allowlist is refused
      (:class:`PolicyDenied`) — a local process cannot filter egress
      by hostname, exactly the docker backend's rule. Open egress
      exists only as ``FULL_ACCESS`` + ``("*",)``;
    * process discipline mirrors the run-lease stores: the backend
      records its creating pid and asserts it on every execution, a
      per-root ``flock`` claim proves single-process ownership, and
      ``os.register_at_fork`` invalidates inherited instances — a
      forked child must construct its own backend.

    Plain ``argv`` executions under a writable policy still get no
    filesystem confinement beyond the working directory and the
    rlimits: that honesty is exactly why this backend is gated to
    explicit dev/test opt-in and refused in production.
    Kernel-grade isolation is the docker backend's job.
    """

    name = "local"

    def __init__(self, workspace_manager: WorkspaceManager) -> None:
        super().__init__(workspace_manager)
        self._created_pid = os.getpid()
        _LOCAL_BACKEND_INSTANCES.add(self)
        _claim_local_root(workspace_manager.root)

    def _assert_usable(self) -> None:
        """Refuse to serve from a backend inherited across ``fork()``
        (the run-lease stores' PID discipline), and re-assert this
        process's flock claim on the backend's root."""
        pid = os.getpid()
        if pid != self._created_pid:
            raise SandboxConfigurationError(
                "local sandbox backend used from a different process than "
                f"the one that created it (created by pid {self._created_pid}, "
                f"called from pid {pid}): a forked child inherits a half-valid "
                "backend and must construct its own instead of using the "
                "inherited one"
            )
        _claim_local_root(self._workspaces.root)

    def available(self) -> bool:
        return local_sandbox_allowed()

    def execute(self, request: SandboxRequest) -> SandboxResult:
        self._assert_usable()
        return super().execute(request)

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
        self._assert_usable()
        # --- network policy, docker-aligned (fail closed) -------------- #
        # Empty allowlist ⇒ no network. Open egress exists only as
        # FULL_ACCESS + ("*",). Anything else names hosts a local
        # process cannot filter by — refused, never silently widened
        # (the pre-hardening behaviour ran code payloads with a fully
        # open network whenever the allowlist was non-empty).
        allowlist = tuple(request.policy.network_allowlist)
        open_network = (
            allowlist == (OPEN_NETWORK,) and request.policy.level is SandboxPolicy.FULL_ACCESS
        )
        if allowlist and not open_network:
            raise PolicyDenied(
                "the local sandbox backend cannot enforce a hostname egress "
                f"allowlist {allowlist!r} (a local process has no egress "
                "filtering); refusing rather than running with silently "
                "widened network access — use an empty allowlist (no network) "
                "or FULL_ACCESS with ('*',) for open egress"
            )
        read_only = request.policy.level is SandboxPolicy.READ_ONLY
        env = local_env(request.env)
        run_argv: tuple[str, ...] = argv
        if request.code is not None:
            # Converge on the native sandbox's guard bootstrap: the
            # payload runs with socket egress blocked (empty allowlist)
            # and writes confined to the workspace, exactly the
            # semantics forgeflow.skills.sandbox_isolated enforces.
            # READ_ONLY additionally flips the guard into deny-all
            # mode: writes are refused even *inside* the workspace.
            from forgeflow.skills.sandbox_isolated import _BOOTSTRAP_SRC

            (workspace.root / "__bootstrap__.py").write_text(_BOOTSTRAP_SRC, encoding="utf-8")
            env["FF_SANDBOX_WORKDIR"] = str(workspace.root)
            env["FF_SANDBOX_NETWORK_BLOCK"] = "0" if open_network else "1"
            env["FF_SANDBOX_WRITE_GUARD"] = "1"
            env["FF_SANDBOX_READ_ONLY"] = "1" if read_only else "0"
            run_argv = self._guarded_code_argv(workspace)
        # --- no-network namespace (docker --network none parity) ------- #
        if not open_network:
            prefix = _netns_prefix()
            if prefix is not None:
                # Kernel-level no-network for the whole tree (covers
                # children the interpreter guard cannot see). For code
                # payloads it is defence in depth over the guard.
                run_argv = (*prefix, *run_argv)
            elif request.code is None:
                raise PolicyDenied(
                    "the local sandbox backend cannot enforce the no-network "
                    "default for an argv payload on this host (no network "
                    "namespace available via unshare, and an argv payload "
                    "has no interpreter guard); refusing to run it with "
                    "silently open network access"
                )
        popen_kwargs: dict[str, Any] = {
            "cwd": str(cwd),
            "env": env,
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
        }
        if os.name == "nt":  # pragma: no cover — Windows process group
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            popen_kwargs["start_new_session"] = True
            popen_kwargs["preexec_fn"] = _local_preexec(request.limits, read_only=read_only)
        proc = subprocess.Popen(list(run_argv), **popen_kwargs)  # noqa: S603
        try:
            outcome = run_bounded(
                proc,
                stdin_data=request.stdin,
                timeout_s=request.limits.timeout_s,
                cap_bytes=request.limits.max_output_chars,
                kill=lambda: self._kill_tree(proc),
                monitor=workspace_disk_monitor(
                    self._workspaces, workspace, request.limits.disk_bytes
                ),
            )
        finally:
            # No exit path (timeout, quota kill, supervisor error) may
            # leave the payload's tree running.
            if proc.poll() is None:
                self._kill_tree(proc)
        exit_code = outcome.exit_code
        oom_killed = bool(
            not outcome.timed_out
            and outcome.violation is None
            and exit_code is not None
            and exit_code == -getattr(signal, "SIGKILL", 9)
        )
        return SandboxResult(
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            exit_code=exit_code,
            timed_out=outcome.timed_out,
            oom_killed=oom_killed,
            quota_exceeded="memory" if oom_killed else outcome.violation,
            stdout_truncated=outcome.stdout_truncated,
            stderr_truncated=outcome.stderr_truncated,
        )

    @staticmethod
    def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
        """Kill the child's **whole tree**, not just its process group.

        Descendants are discovered (``/proc`` parent-chain, see
        :func:`_descendant_pids`) *before* any kill lands — afterwards
        the chain is broken by reparenting. The group kill still
        fires (it is the fast path for the common case), and every
        discovered descendant is killed individually, so a grandchild
        that escaped the group via ``setsid`` dies too. Windows:
        ``taskkill /T`` best-effort.
        """
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
        descendants = _descendant_pids(proc.pid)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        for pid in descendants:
            if pid == proc.pid:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass


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
