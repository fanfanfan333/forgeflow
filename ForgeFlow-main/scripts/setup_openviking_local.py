"""Prepare an isolated local OpenViking container without changing app packages."""

from __future__ import annotations

import json
import secrets
import subprocess
from pathlib import Path

import httpx


def main():
    models = httpx.get("http://127.0.0.1:11434/api/tags", trust_env=False, timeout=20)
    models.raise_for_status()
    installed = {model["name"].removesuffix(":latest") for model in models.json()["models"]}
    for required in ("qwen3:4b", "qwen2.5vl:3b", "nomic-embed-text"):
        if required not in installed:
            pulled = httpx.post(
                "http://127.0.0.1:11434/api/pull",
                json={"model": required, "stream": False},
                trust_env=False,
                timeout=1800,
            )
            pulled.raise_for_status()
    response = httpx.post(
        "http://127.0.0.1:11434/api/create",
        json={
            "model": "forgeflow-viking-qwen3-4b",
            "from": "qwen3:4b",
            "parameters": {"num_ctx": 16384, "temperature": 0},
            "stream": False,
        },
        trust_env=False,
        timeout=120,
    )
    response.raise_for_status()
    root = Path(".runtime/openviking").resolve()
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / "ov.conf"
    if not config_path.exists():
        config = {
            "embedding": {
                "dense": {
                    "provider": "openai",
                    "api_base": "http://host.docker.internal:11434/v1",
                    "api_key": "local-ollama",
                    "model": "nomic-embed-text",
                    "dimension": 768,
                }
            },
            "vlm": {
                "provider": "openai",
                "api_base": "http://host.docker.internal:1934/v1",
                "api_key": secrets.token_urlsafe(48),
                "model": "forgeflow-viking-qwen3-4b",
                "temperature": 0,
                "max_tokens": 4096,
                "extra_request_body": {"reasoning_effort": "none"},
                "reasoning_effort": "none",
                "timeout": 600,
                "max_concurrent": 1,
            },
            "storage": {"workspace": "/data/workspace"},
            "server": {"host": "0.0.0.0", "port": 1933, "root_api_key": secrets.token_urlsafe(48)},
            "memory": {
                "extraction_enabled": True,
                "extraction_output_format": "json",
                "session_skill_extraction_enabled": False,
            },
        }
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    from start_viking_adapter import ensure_adapter

    ensure_adapter()
    image = (
        "openviking-cn-beijing.cr.volces.com/volcengine/openviking@sha256:"
        "fb4cd789fd3303509edd1c10437ff63d4fab56b622494ac621f1fda042540ed4"
    )
    if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode:
        subprocess.run(["docker", "pull", image], check=True)
    digest = subprocess.check_output(
        ["docker", "image", "inspect", image, "--format", "{{index .RepoDigests 0}}"], text=True
    ).strip()
    (root / "image-digest.txt").write_text(digest, encoding="utf-8")
    existing = subprocess.run(["docker", "inspect", "forgeflow-openviking"], capture_output=True)
    if existing.returncode:
        volume = (
            (root / "volume-name.txt").read_text(encoding="utf-8").strip()
            if (root / "volume-name.txt").exists()
            else "forgeflow-openviking-data"
        )
        subprocess.run(["docker", "volume", "create", volume], check=True)
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                "forgeflow-openviking",
                "--restart",
                "unless-stopped",
                "-p",
                "127.0.0.1:1933:1933",
                "-v",
                volume + ":/data",
                "-v",
                f"{config_path}:/app/.openviking/ov.conf:ro",
                "-e",
                "OPENVIKING_CONFIG_FILE=/app/.openviking/ov.conf",
                digest,
            ],
            check=True,
        )
    else:
        subprocess.run(["docker", "start", "forgeflow-openviking"], check=True)
    print("OpenViking container prepared; credentials stay in the ignored runtime directory.")


if __name__ == "__main__":
    main()
