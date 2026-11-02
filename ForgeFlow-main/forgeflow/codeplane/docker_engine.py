"""Run the existing JSONL/OpenHands runner in a bounded Docker container."""

from __future__ import annotations

import asyncio
import secrets
import shutil
import subprocess
import time
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
        self._networks: dict[str, tuple[str, str, str]] = {}
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
        gateway = self._networks.get(job.run_id)
        if gateway is None:
            raise ValueError("Isolated model gateway is not available")
        network, _, secret = gateway
        base_url = f"http://model-proxy:18765/{secret}"
        test = job.test_command
        if test == getattr(self._settings, "codeplane_test_command", ""):
            test = str(getattr(self._settings, "codeplane_docker_test_command", "python -m pytest -q"))
        mapped = replace(job, workspace_path="/workspace", base_url=base_url, test_command=test)
        mapped._project_checks = getattr(job, "_project_checks", {})
        mapped._container_image_id = self._image_id or None
        argv = [self._interpreter, "run", "--rm", "--interactive", "--pull=never", "--name", name,
                "--label", "forgeflow.codeplane=true", "--label", f"forgeflow.run_id={job.run_id}", "--cap-drop=ALL",
                "--security-opt=no-new-privileges", "--read-only", "--memory=4g", "--cpus=2",
                "--network", network,
                "--pids-limit=256", "--user=1000:1000", "--workdir=/workspace",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m,mode=1777",
                "--env", "HOME=/tmp", "--env", "CI=1",
                "--env", "LITELLM_LOCAL_MODEL_COST_MAP=true",
                "--env", "FORGEFLOW_CODEPLANE_CONTAINER=1", "--env", "OPENHANDS_SUPPRESS_BANNER=1",
                "--mount", f"type=bind,source={root},target=/workspace",
                "--mount", f"type=bind,source={root / '.git'},target=/workspace/.git,readonly",
                "--mount", f"type=bind,source={_DEFAULT_RUNNER.parent},target=/opt/forgeflow-runner,readonly",
                self._image_id or self._image, "python", "-u", "/opt/forgeflow-runner/container_bootstrap.py"]
        self._containers[job.run_id] = name
        return argv, mapped

    async def run(self, job: CodeJob) -> CodeRunResult:
        isolated = False
        if not self.available():
            result = self._degraded("engine_unavailable", self._reason)
        else:
            job = self._merge_defaults(job)
            try:
                setup = asyncio.create_task(asyncio.to_thread(self._prepare_model_gateway, job))
                try:
                    await asyncio.shield(setup)
                    isolated = True
                except asyncio.CancelledError:
                    try:
                        await setup
                    except Exception:
                        pass
                    raise
                result = await super().run(job)
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                result = self._degraded("engine_unavailable", f"Model isolation unavailable: {exc}")
            finally:
                await asyncio.shield(self._cleanup_model_gateway(job))
        result.transport = "docker"
        result.raw["isolation"] = {"filesystem": "container", "network": "internal-model-gateway" if isolated else "unavailable",
                                   "image": self._image, "image_id": result.raw.get("container_image_id"),
                                   "host_runtime_fallback": False}
        return result

    def _prepare_model_gateway(self, job: CodeJob) -> None:
        if not job.run_id or job.run_id in self._networks:
            raise ValueError("Model gateway requires a unique run ID")
        endpoint = urlsplit(job.base_url)
        if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname or endpoint.username
                or endpoint.password or endpoint.query or endpoint.fragment):
            raise ValueError("Model endpoint must be an HTTP URL without embedded credentials")
        if endpoint.hostname in {"localhost", "127.0.0.1", "::1"}:
            authority = "host.docker.internal" + (f":{endpoint.port}" if endpoint.port else "")
            upstream = urlunsplit(endpoint._replace(netloc=authority))
        else:
            upstream = job.base_url
        ident = uuid4().hex
        network, proxy, secret = "forgeflow-model-net-" + ident, "forgeflow-model-proxy-" + ident, secrets.token_urlsafe(32)
        self._networks[job.run_id] = (network, proxy, secret)
        proxy_file = Path(__file__).with_name("model_proxy.py").resolve()
        if "," in str(proxy_file):
            raise ValueError("Model gateway path cannot contain a comma")
        env = self._env()
        def docker(*args: str) -> None:
            subprocess.run([self._interpreter, *args], env=env, capture_output=True, check=True, timeout=20)
        docker("network", "create", "--internal", network)
        ttl = min(86400, max(1, int(job.wall_timeout_s or getattr(self._settings, "codeplane_timeout_seconds", 180)) + 60))
        docker("create", "--rm", "--name", proxy, "--network", "bridge", "--pull=never", "--cap-drop=ALL",
               "--security-opt=no-new-privileges", "--read-only", "--user=1000:1000",
               "--memory=256m", "--pids-limit=64", "--add-host", "host.docker.internal:host-gateway",
               "--env", f"MODEL_UPSTREAM={upstream}", "--env", f"MODEL_PROXY_SECRET={secret}",
               "--env", f"MODEL_PROXY_MAX_SECONDS={ttl}",
               "--mount", f"type=bind,source={proxy_file},target=/model_proxy.py,readonly",
               self._image_id, "python", "-u", "/model_proxy.py")
        docker("network", "connect", "--alias", "model-proxy", network, proxy)
        docker("start", proxy)
        deadline = time.monotonic() + 10
        while True:
            probe = subprocess.run(
                [self._interpreter, "exec", proxy, "python", "-c",
                 "import socket; socket.create_connection(('127.0.0.1', 18765), 1).close()"],
                env=env, capture_output=True, timeout=5, check=False,
            )
            if probe.returncode == 0:
                break
            if time.monotonic() >= deadline:
                raise OSError("Model gateway failed to become ready")
            time.sleep(0.2)

    async def _cleanup_model_gateway(self, job: CodeJob) -> None:
        entry = self._networks.get(job.run_id)
        if entry is None:
            return
        network, proxy, _ = entry
        env = self._env()
        for args in (("rm", "--force", proxy), ("network", "rm", network)):
            try:
                result = await asyncio.to_thread(subprocess.run, [self._interpreter, *args], capture_output=True, env=env, timeout=20, check=False)
                if result.returncode:
                    inspect = ("container", "inspect", proxy) if args[0] == "rm" else ("network", "inspect", network)
                    exists = await asyncio.to_thread(subprocess.run, [self._interpreter, *inspect], capture_output=True, env=env, timeout=10, check=False)
                    daemon = await asyncio.to_thread(subprocess.run, [self._interpreter, "info", "--format", "{{.ServerVersion}}"], capture_output=True, env=env, timeout=10, check=False)
                    if exists.returncode == 0 or daemon.returncode != 0:
                        raise ContainerCleanupUnverified("Model gateway cleanup could not be confirmed")
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ContainerCleanupUnverified("Model gateway cleanup could not be confirmed") from exc
        self._networks.pop(job.run_id, None)

    def _parse(self, stdout: str, stderr: str, exit_code: int, job: CodeJob) -> CodeRunResult:
        gateway = self._networks.get(job.run_id)
        if gateway:
            stdout = stdout.replace(gateway[2], "[redacted]")
            stderr = stderr.replace(gateway[2], "[redacted]")
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
