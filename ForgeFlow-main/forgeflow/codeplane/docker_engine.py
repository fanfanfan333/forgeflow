"""Run the existing JSONL/OpenHands runner in a bounded Docker container."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from forgeflow.codeplane.engine import (
    _DEFAULT_RUNNER,
    CodeJob,
    CodeRunResult,
    SubprocessOpenHandsEngine,
)
from forgeflow.codeplane.workspace import _is_within_project


class ContainerCleanupUnverified(RuntimeError):
    """The daemon could not confirm that the task container is gone."""


class DockerOpenHandsEngine(SubprocessOpenHandsEngine):
    def __init__(self, settings: Any = None) -> None:
        super().__init__(interpreter=shutil.which("docker") or "", settings=settings)
        self._image = str(getattr(self._settings, "codeplane_docker_image", "") or "").strip()
        self._containers: dict[str, str] = {}
        self._image_id = ""

    def available(self) -> bool:
        if not self._enabled or not self._interpreter or not self._image or self._image.startswith("-"):
            self._reason = "Docker code runtime is disabled or not configured"
            return False
        try:
            result = subprocess.run([self._interpreter, "image", "inspect", "--format", "{{.Id}}", self._image],
                                    capture_output=True, text=True,
                                    env=self._env(), timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            self._reason = "Docker daemon or configured runner image is unavailable"
            return False
        if result.returncode:
            self._reason = "Build the configured code runtime image first; no host-process fallback is permitted"
            return False
        image_id = result.stdout.strip()
        if not image_id.startswith("sha256:") or len(image_id) != 71:
            self._reason = "Docker image identity could not be verified"
            return False
        self._image_id = image_id
        self._reason = ""
        return True

    def _launch(self, job: CodeJob) -> tuple[list[str], CodeJob]:
        root = Path(job.workspace_path).resolve()
        if (not root.is_dir() or not (root / ".git").is_dir() or (root / ".git").is_symlink()
                or _is_within_project(root) or "," in str(root)
                or "," in str(_DEFAULT_RUNNER.parent)):
            raise ValueError("Docker requires an external task workspace with a valid mount path")
        name = "forgeflow-code-" + uuid4().hex
        endpoint = urlsplit(job.base_url)
        base_url = job.base_url
        if endpoint.hostname in {"localhost", "127.0.0.1", "::1"}:
            if endpoint.username or endpoint.password:
                raise ValueError("Local model endpoints must not embed credentials")
            authority = "host.docker.internal" + (f":{endpoint.port}" if endpoint.port else "")
            base_url = urlunsplit(endpoint._replace(netloc=authority))
        test = job.test_command
        if test == getattr(self._settings, "codeplane_test_command", ""):
            test = str(getattr(self._settings, "codeplane_docker_test_command", "python -m pytest -q"))
        mapped = replace(job, workspace_path="/workspace", base_url=base_url, test_command=test)
        mapped._project_checks = getattr(job, "_project_checks", {})
        mapped._container_image_id = self._image_id or None
        argv = [self._interpreter, "run", "--rm", "--interactive", "--pull=never", "--name", name,
                "--label", "forgeflow.codeplane=true", "--label", f"forgeflow.run_id={job.run_id}", "--cap-drop=ALL",
                "--security-opt=no-new-privileges", "--read-only", "--memory=4g", "--cpus=2",
                "--pids-limit=256", "--user=1000:1000", "--workdir=/workspace",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m,mode=1777",
                "--env", "HOME=/tmp", "--env", "CI=1",
                "--env", "FORGEFLOW_CODEPLANE_CONTAINER=1", "--env", "OPENHANDS_SUPPRESS_BANNER=1",
                "--add-host", "host.docker.internal:host-gateway",
                "--mount", f"type=bind,source={root},target=/workspace",
                "--mount", f"type=bind,source={root / '.git'},target=/workspace/.git,readonly",
                "--mount", f"type=bind,source={_DEFAULT_RUNNER.parent},target=/opt/forgeflow-runner,readonly",
                self._image_id or self._image, "python", "-u", "/opt/forgeflow-runner/container_bootstrap.py"]
        self._containers[job.run_id] = name
        return argv, mapped

    async def run(self, job: CodeJob) -> CodeRunResult:
        result = await super().run(job)
        result.transport = "docker"
        result.raw["isolation"] = {"filesystem": "container", "network": "bridge",
                                   "image": self._image, "image_id": result.raw.get("container_image_id"),
                                   "host_runtime_fallback": False}
        return result

    def _parse(self, stdout: str, stderr: str, exit_code: int, job: CodeJob) -> CodeRunResult:
        result = super()._parse(stdout, stderr, exit_code, job)
        result.raw["container_image_id"] = getattr(job, "_container_image_id", None)
        return result

    async def _cleanup(self, job: CodeJob) -> None:
        name = self._containers.get(job.run_id)
        if not name:
            return
        env = self._env()
        try:
            result = await asyncio.to_thread(subprocess.run, [self._interpreter, "rm", "--force", name],
                                            capture_output=True, env=env, timeout=15, check=False)
            if result.returncode:
                check = await asyncio.to_thread(subprocess.run,
                                                [self._interpreter, "ps", "--all", "--filter", f"name=^/{name}$", "--format", "{{.ID}}"],
                                                capture_output=True, env=env, timeout=5, check=False)
                if check.returncode != 0 or check.stdout.strip():
                    raise ContainerCleanupUnverified("Code container cleanup could not be confirmed")
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ContainerCleanupUnverified("Code container cleanup could not be confirmed") from exc
        self._containers.pop(job.run_id, None)
