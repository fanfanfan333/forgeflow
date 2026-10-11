"""Start the authenticated model bridge once; preserve another listener on the port."""

import subprocess
import sys
import time
from pathlib import Path

import httpx


def ensure_adapter():
    root = Path(__file__).resolve().parents[1]
    try:
        response = httpx.get("http://127.0.0.1:1934/health", trust_env=False, timeout=2)
        if response.json().get("service") != "forgeflow-viking-model-adapter":
            raise RuntimeError("Adapter port is occupied by another service")
        return
    except (httpx.ConnectError, httpx.ConnectTimeout):
        pass
    log_path = root / ".runtime/openviking/model-adapter.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        subprocess.Popen(
            [sys.executable, str(root / "scripts/run_viking_model_adapter.py")],
            cwd=root,
            stdout=log,
            stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    for _ in range(20):
        try:
            response = httpx.get("http://127.0.0.1:1934/health", trust_env=False, timeout=1)
            response.raise_for_status()
            return
        except httpx.HTTPError:
            time.sleep(0.25)
    raise RuntimeError("Model adapter did not become ready")


if __name__ == "__main__":
    ensure_adapter()
