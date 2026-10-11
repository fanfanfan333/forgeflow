"""Platform-independent subprocess execution (layer L1, §80).

The single place in the QA package that actually *runs* something. Everything
about it is chosen so the same behaviour holds on Windows (local) and on Linux
(CI):

* ``shell=False`` with a ``list[str]`` argv — no ``sh``/``cmd.exe`` string
  joining, no shell-injection surface, no per-platform quoting differences.
* ``sys.executable`` is supplied by callers; the module never assumes a bare
  ``python`` on ``PATH`` (which on Windows can be the Store stub that exits 9009).
* Every call carries a real ``timeout`` (§80): a hung child is turned into a
  ``TIMEOUT`` result, never an infinite block.
* Failures (non-zero exit, timeout, spawn error) are *data*, captured into a
  :class:`ProcessResult` — this method does not raise on a child failure.

Only *programming* mistakes (a malformed ``command``) raise.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from forgeflow.qa.config import DEFAULT_TIMEOUT_S
from forgeflow.qa.models import ProcessResult, ProcessStatus

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


def _as_text(value: bytes | str | None) -> str:
    """Normalise a possibly-bytes/None captured stream to ``str``."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


class ProcessRunner:
    """Runs argv-style subprocesses and returns their *real* result."""

    def __init__(
        self,
        *,
        default_timeout_s: int = DEFAULT_TIMEOUT_S,
        env_overrides: dict[str, str] | None = None,
    ) -> None:
        self._default_timeout_s = default_timeout_s
        self._env_overrides = dict(env_overrides or {})

    @property
    def default_timeout_s(self) -> int:
        """The timeout applied when a call does not specify one."""
        return self._default_timeout_s

    def run(
        self,
        command: list[str],
        *,
        cwd: Path | str | None = None,
        timeout_s: int | None = None,
        env: dict[str, str] | None = None,
    ) -> ProcessResult:
        """Execute ``command`` and capture everything that matters (§80).

        ``command`` must be a non-empty ``list[str]``. ``env`` (when given) is
        merged on top of ``os.environ`` ∪ the runner's own ``env_overrides``; only
        the keys that differ from ``os.environ`` are recorded in ``env_diff``.
        """
        if not isinstance(command, list) or not command:
            raise ValueError("command must be a non-empty list[str]")
        if not all(isinstance(part, str) for part in command):
            raise ValueError("every element of command must be a str")

        cwd_path = Path(cwd).resolve() if cwd is not None else Path.cwd()
        timeout = timeout_s if timeout_s is not None else self._default_timeout_s

        parent_env = dict(os.environ)
        child_env = dict(parent_env)
        child_env.update(self._env_overrides)
        if env:
            child_env.update(env)
        env_diff = {k: v for k, v in child_env.items() if parent_env.get(k) != v}

        started_at = _utc_now_iso()
        start = time.perf_counter()

        try:
            # argv list + shell=False: no shell interpretation, no injection surface.
            completed = subprocess.run(
                command,
                cwd=str(cwd_path),
                env=child_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            duration_ms = int((time.perf_counter() - start) * 1000)
            logger.warning("subprocess timed out after %ss: %s", timeout, command)
            return ProcessResult(
                command=command,
                cwd=str(cwd_path),
                env_diff=env_diff,
                exit_code=None,
                status=ProcessStatus.TIMEOUT,
                stdout=_as_text(exc.stdout),
                stderr=_as_text(exc.stderr),
                duration_ms=duration_ms,
                started_at=started_at,
                timed_out=True,
                timeout_s=timeout,
            )
        except OSError as exc:
            duration_ms = int((time.perf_counter() - start) * 1000)
            logger.warning("subprocess failed to start: %s (%s)", command, exc)
            return ProcessResult(
                command=command,
                cwd=str(cwd_path),
                env_diff=env_diff,
                exit_code=None,
                status=ProcessStatus.SPAWN_ERROR,
                stdout="",
                stderr=f"{type(exc).__name__}: {exc}",
                duration_ms=duration_ms,
                started_at=started_at,
                timed_out=False,
                timeout_s=timeout,
            )

        duration_ms = int((time.perf_counter() - start) * 1000)
        status = ProcessStatus.OK if completed.returncode == 0 else ProcessStatus.FAILED
        return ProcessResult(
            command=command,
            cwd=str(cwd_path),
            env_diff=env_diff,
            exit_code=completed.returncode,
            status=status,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            duration_ms=duration_ms,
            started_at=started_at,
            timed_out=False,
            timeout_s=timeout,
        )


__all__ = ["ProcessRunner"]
