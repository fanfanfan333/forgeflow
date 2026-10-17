"""Environment guard — separate "the machine is unwell" from "the code is wrong".

Layer L2 (§41). Before a failure is ever classified as a ``SOURCE_BUG``, the
guard asks whether the *environment* can even support the test: is the running
interpreter real (not the Windows Store alias), are the core dependencies
importable, are the optional services (Ollama / PostgreSQL) reachable, is a
browser stack present?

Design rules:

* **A blocking finding routes to ``ENVIRONMENT_FAILURE``, never ``SOURCE_BUG``.**
  The classifier consumes these findings; the guard itself only reports facts.
* **Only genuinely required things are ``blocking``.** A missing interpreter or
  core dependency is ``blocking``; an absent optional service or browser is a
  ``warning`` (the fast profile does not need them). Anything not verified is
  reported honestly rather than assumed healthy.
* **Collection never raises.** Probing an unreachable host or a missing module
  returns a finding, it does not abort the run.
"""

from __future__ import annotations

import importlib.util
import logging
import shutil
import socket
import sys
from typing import Literal

from pydantic import BaseModel

from forgeflow.qa.config import QaRunConfig
from forgeflow.qa.models import ProcessStatus
from forgeflow.qa.process_runner import ProcessRunner

logger = logging.getLogger(__name__)

EnvCheck = Literal[
    "wrong_python",
    "missing_dependency",
    "service_unavailable",
    "port_in_use",
    "browser_unavailable",
]
EnvSeverity = Literal["info", "warning", "blocking"]

#: Core modules whose absence makes the run untrustworthy (blocking).
_CORE_DEPENDENCIES: tuple[str, ...] = ("pytest", "pydantic")

#: Optional service probes: (label, host, port). Absent ⇒ warning, not blocking.
_SERVICE_PROBES: tuple[tuple[str, str, int], ...] = (
    ("ollama", "localhost", 11434),
    ("postgres", "localhost", 5433),
)


class EnvFinding(BaseModel):
    """One environment probe result. ``ok=False`` + ``blocking`` ⇒ env failure."""

    check: EnvCheck
    ok: bool
    detail: str
    severity: EnvSeverity


class EnvironmentGuard:
    """Runs the environment probes for one QA run."""

    def __init__(self, config: QaRunConfig) -> None:
        self._config = config

    def check_python(self) -> EnvFinding:
        """Confirm ``sys.executable`` is a real interpreter (not the Store stub)."""
        if "windowsapps" in sys.executable.lower():
            return EnvFinding(
                check="wrong_python",
                ok=False,
                severity="blocking",
                detail=(
                    f"{sys.executable} is the Windows Store app-alias stub, which "
                    "exits silently — configure a real interpreter."
                ),
            )
        runner = ProcessRunner(default_timeout_s=30)
        process = runner.run([sys.executable, "-VV"])
        output = (process.stdout + process.stderr).strip()
        if process.status is not ProcessStatus.OK or process.exit_code != 0 or not output:
            return EnvFinding(
                check="wrong_python",
                ok=False,
                severity="blocking",
                detail=(
                    f"{sys.executable} failed to report a version "
                    f"(status={process.status}, exit={process.exit_code})."
                ),
            )
        first_line = output.splitlines()[0]
        return EnvFinding(
            check="wrong_python",
            ok=True,
            severity="info",
            detail=f"{sys.executable}: {first_line}",
        )

    def check_dependency(self, module: str, *, severity: EnvSeverity = "blocking") -> EnvFinding:
        """Report whether ``module`` is importable on this interpreter."""
        try:
            spec = importlib.util.find_spec(module)
        except (ImportError, ValueError) as exc:
            return EnvFinding(
                check="missing_dependency",
                ok=False,
                severity=severity,
                detail=f"{module}: not importable ({type(exc).__name__}: {exc})",
            )
        if spec is None:
            return EnvFinding(
                check="missing_dependency",
                ok=False,
                severity=severity,
                detail=f"{module}: module not found",
            )
        origin = getattr(spec, "origin", None) or "unknown"
        return EnvFinding(
            check="missing_dependency",
            ok=True,
            severity="info",
            detail=f"{module}: {origin}",
        )

    def check_service(
        self,
        host: str,
        port: int,
        *,
        timeout_s: float = 1.0,
        severity: EnvSeverity = "warning",
        label: str | None = None,
    ) -> EnvFinding:
        """Report whether ``host:port`` accepts a TCP connection."""
        name = label or f"{host}:{port}"
        try:
            with socket.create_connection((host, port), timeout=timeout_s):
                pass
        except OSError as exc:
            return EnvFinding(
                check="service_unavailable",
                ok=False,
                severity=severity,
                detail=f"{name} unreachable ({type(exc).__name__}: {exc})",
            )
        return EnvFinding(
            check="service_unavailable",
            ok=True,
            severity="info",
            detail=f"{name} reachable",
        )

    def check_port(self, host: str, port: int, *, timeout_s: float = 0.5) -> EnvFinding:
        """Report whether ``host:port`` is free (``ok=True``) or already in use."""
        try:
            with socket.create_connection((host, port), timeout=timeout_s):
                pass
        except OSError:
            return EnvFinding(
                check="port_in_use",
                ok=True,
                severity="info",
                detail=f"{host}:{port} is free",
            )
        return EnvFinding(
            check="port_in_use",
            ok=False,
            severity="warning",
            detail=f"{host}:{port} is already in use",
        )

    def check_browser(self) -> EnvFinding:
        """Report whether a browser stack (node, optionally Playwright) exists."""
        node = shutil.which("node")
        playwright = self.check_dependency("playwright", severity="warning")
        if node:
            return EnvFinding(
                check="browser_unavailable",
                ok=True,
                severity="info",
                detail=f"node={node}; playwright={'present' if playwright.ok else 'absent'}",
            )
        return EnvFinding(
            check="browser_unavailable",
            ok=False,
            severity="warning",
            detail="node not found on PATH; browser-based (UI/E2E) tests cannot run",
        )

    def run_all(self) -> list[EnvFinding]:
        """Run the standard probe set for a QA run."""
        findings: list[EnvFinding] = [self.check_python()]
        findings.extend(self.check_dependency(module) for module in _CORE_DEPENDENCIES)
        findings.extend(
            self.check_service(host, port, label=label) for label, host, port in _SERVICE_PROBES
        )
        findings.append(self.check_browser())
        blocking = [f for f in findings if not f.ok and f.severity == "blocking"]
        if blocking:
            logger.warning("environment guard found %d blocking issue(s)", len(blocking))
        return findings


__all__ = ["EnvCheck", "EnvFinding", "EnvSeverity", "EnvironmentGuard"]
