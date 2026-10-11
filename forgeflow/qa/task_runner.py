"""Make-backed ``TaskRunnerAdapter`` (§15/§78).

Make is ForgeFlow's *only* task-orchestration layer, so the adapter is thin: it
parses the ``Makefile`` for real targets, cross-checks them against a whitelist,
and runs a target through :class:`ProcessRunner` as ``make PY=<interpreter>
<target>``.

Two invariants matter:

* **No phantom targets.** :meth:`MakeTaskRunnerAdapter.has_target` is the single
  existence gate; :meth:`run_task` refuses a target that is not in the parsed
  Makefile, so a plan can never reference (and "pass") a target that does not
  exist (§15). The Makefile has no ``integration`` target — the real name is
  ``test-integration`` — and the discovery logic must never invent it.
* **The interpreter is always explicit.** The argv carries ``PY=<sys.executable>``
  because the Makefile's recipes shell out via ``$(PY)``; on Windows a bare
  ``python`` on ``PATH`` is frequently the Store stub (§0.6).
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Protocol, runtime_checkable

from forgeflow.qa.models import ProcessStatus, TaskResult, TaskSpec
from forgeflow.qa.process_runner import ProcessRunner

logger = logging.getLogger(__name__)

#: A target line: ``name:`` (not ``name := ...``, not ``name = ...``). The
#: negative lookahead excludes the ``:=`` assignment operator; a leading-letter
#: anchor excludes ``.PHONY`` and ``_``-prefixed sentinels.
_TARGET_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9_.-]*)\s*:(?!=)")

#: Real, whitelisted Make targets. A target is discovered iff it appears as a
#: rule in the Makefile *and* is in this set, so incidental or helper rules can
#: never leak into a test plan.
KNOWN_TARGETS: frozenset[str] = frozenset(
    {
        "help",
        "install",
        "lint",
        "lint-all",
        "lint-gate",
        "lint-ratchet",
        "format",
        "format-check",
        "format-all",
        "typecheck",
        "typecheck-all",
        "typecheck-gate",
        "test",
        "test-unit",
        "test-integration",
        "test-fast",
        "test-all",
        "check",
        "check-full",
        "test-document-delivery",
        "realstack",
        "backend",
        "backend-reload",
        "env",
        "clean",
        "build",
        "frontend-install",
        "frontend-build",
        "frontend-lint",
        "frontend-test",
        "db-up",
        "db-down",
        "migrate",
        "install-extras",
        "cov",
    }
)


def parse_makefile_targets(text: str) -> dict[str, str]:
    """Parse ``{target: description}`` from Makefile ``text``.

    The description is the contiguous block of ``#`` comments immediately above
    the target rule (blank lines separate blocks). Recipe lines (leading tab) and
    assignments are ignored, so ``VAR := value`` never registers as a target.
    """
    targets: dict[str, str] = {}
    comments: list[str] = []
    for raw in text.splitlines():
        if not raw:
            comments = []
            continue
        if raw.startswith("\t"):
            continue  # a recipe line — keep any pending comment block
        stripped = raw.strip()
        if stripped.startswith("#"):
            comments.append(stripped.lstrip("#").strip())
            continue
        match = _TARGET_LINE.match(raw)
        if match:
            name = match.group(1)
            if name not in targets:
                targets[name] = " ".join(part for part in comments if part).strip()
            comments = []
            continue
        comments = []  # any other line breaks the comment block
    return targets


@runtime_checkable
class TaskRunnerAdapter(Protocol):
    """The task-runner contract the planner/executor depend on (§15)."""

    def discover_tasks(self) -> list[TaskSpec]:
        """Return the discoverable tasks for this runner."""
        ...

    def run_task(self, task: TaskSpec, *, run_id: str) -> TaskResult:
        """Run ``task`` and return its result (never raises on a task failure)."""
        ...

    def get_result(self, target: str) -> TaskResult | None:
        """Return the last result for ``target``, or ``None``."""
        ...

    def has_target(self, target: str) -> bool:
        """True when ``target`` exists and may be run (§15 existence gate)."""
        ...

    def command_for(self, target: str) -> list[str]:
        """The argv that would run ``target`` (for plan ``command`` fields)."""
        ...


class MakeTaskRunnerAdapter:
    """Concrete :class:`TaskRunnerAdapter` backed by a ``Makefile``."""

    def __init__(
        self,
        makefile: Path,
        python: str,
        runner: ProcessRunner,
        *,
        make_exe: str | None = None,
    ) -> None:
        self._makefile = Path(makefile)
        self._python = python
        self._runner = runner
        self._make_exe = make_exe or shutil.which("make") or "make"
        self._specs: dict[str, TaskSpec] = {}
        self._results: dict[str, TaskResult] = {}

    @property
    def makefile(self) -> Path:
        """The ``Makefile`` this adapter parses and runs."""
        return self._makefile

    @property
    def make_exe(self) -> str:
        """The resolved ``make`` executable (as used in every command)."""
        return self._make_exe

    def discover_tasks(self) -> list[TaskSpec]:
        """Parse the Makefile and return the whitelisted targets it defines."""
        if not self._makefile.is_file():
            raise RuntimeError(f"Makefile not found: {self._makefile}")
        parsed = parse_makefile_targets(self._makefile.read_text(encoding="utf-8"))
        specs = [
            TaskSpec(target=target, description=parsed[target], exists=True)
            for target in sorted(parsed)
            if target in KNOWN_TARGETS
        ]
        self._specs = {spec.target: spec for spec in specs}
        logger.debug("discovered %d Make targets", len(specs))
        return specs

    def has_target(self, target: str) -> bool:
        """True when ``target`` is a real, whitelisted Makefile target."""
        if not self._specs:
            self.discover_tasks()
        return target in self._specs

    def command_for(self, target: str) -> list[str]:
        """The argv that would run ``target`` (``make PY=<python> <target>``)."""
        return [self._make_exe, f"PY={self._python}", target]

    def run_task(self, task: TaskSpec, *, run_id: str) -> TaskResult:
        """Run ``task.target``; refuse any target that does not exist (§15)."""
        if not self.has_target(task.target):
            raise ValueError(
                f"refusing to run unknown Make target {task.target!r}: "
                f"not present in {self._makefile}"
            )
        command = self.command_for(task.target)
        logger.info("run[%s] %s", run_id, " ".join(command))
        process = self._runner.run(command, cwd=self._makefile.parent)
        result = TaskResult(
            target=task.target,
            ok=process.status is ProcessStatus.OK,
            result=process,
        )
        self._results[task.target] = result
        return result

    def get_result(self, target: str) -> TaskResult | None:
        """Return the last :class:`TaskResult` for ``target``, if any."""
        return self._results.get(target)


__all__ = [
    "KNOWN_TARGETS",
    "MakeTaskRunnerAdapter",
    "TaskRunnerAdapter",
    "parse_makefile_targets",
]
