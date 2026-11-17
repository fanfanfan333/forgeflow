"""Isolation backends — Phase 3 unified layout.

* :mod:`forgeflow.sandbox.runtime` — the **unified** one-shot sandbox
  contract (``SandboxRuntime.execute(request) -> SandboxResult``):
  codex-style :class:`~forgeflow.sandbox.runtime.SandboxPolicy` levels,
  :class:`~forgeflow.sandbox.runtime.SandboxLimits`, per-tenant/run
  workspaces with symlink-safe path resolution, the backend factory
  (:func:`~forgeflow.sandbox.runtime.get_sandbox_runtime`, mirroring
  the run-lease store's fail-closed resolution discipline), the gated
  dev/test-only local process backend, and the reserved remote
  (E2B-like) backend stub.
* :mod:`forgeflow.sandbox.docker_backend` — the default Docker backend
  (plus the gVisor ``runsc`` flavour) behind a pure, unit-testable
  argv builder that always emits the full hard-limit set.
* :mod:`forgeflow.sandbox.docker_isolated` — the INC46 container
  backend's original API (``run_isolated_docker`` / capability
  reports), now a thin adapter over the unified Docker backend.
* The **native** pure-Python backend stays in
  :mod:`forgeflow.skills.sandbox_isolated` (degraded on Windows
  without admin): its interpreter-guard bootstrap is shared with the
  unified local backend for Python code payloads, while its own
  supervisor-based runner keeps its historical verdict contract.

The code plane (:mod:`forgeflow.codeplane`) — the agentic substrate
behind ``code.execute`` — is not a one-shot sandbox and is not
replaced by this package; the executor governs it with the same
policy vocabulary (declared per-tool policies + the FULL_ACCESS
approval gate in :mod:`forgeflow.runtime.tool_executor`).

The T15 publish-interlock requirement **R2** anchors on
``forgeflow.sandbox.real_isolation``; that module is deliberately
**not** created here (R2 is kept ``met=False`` per ruling R9 until
the container backend lands and QA re-verifies end to end).
"""

from __future__ import annotations

from forgeflow.sandbox.runtime import (
    DEFAULT_LIMITS,
    DEFAULT_POLICY,
    LocalProcessBackend,
    PolicyDenied,
    RemoteBackend,
    SandboxConfigurationError,
    SandboxError,
    SandboxLimits,
    SandboxPolicy,
    SandboxPolicySpec,
    SandboxRequest,
    SandboxResult,
    SandboxRuntime,
    SandboxUnavailable,
    WorkspaceManager,
    get_sandbox_runtime,
)

__all__ = [
    "DEFAULT_LIMITS",
    "DEFAULT_POLICY",
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
    "WorkspaceManager",
    "get_sandbox_runtime",
]
