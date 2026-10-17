"""Verify the real code engine, diff, tests and approval on a fresh probe repo."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx

from forgeflow.config import get_settings


def main():
    root = Path("_audit") / ("code-verification-" + uuid4().hex[:8])
    root.mkdir(parents=True)
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    (root / "mathlib.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (root / "test_mathlib.py").write_text(
        "from mathlib import add\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8"
    )
    for command in [
        ["git", "init", "-q"],
        ["git", "add", "."],
        [
            "git",
            "-c",
            "user.email=probe@localhost",
            "-c",
            "user.name=verification",
            "commit",
            "-qm",
            "baseline",
        ],
    ]:
        subprocess.run(command, cwd=root, check=True, capture_output=True)
    with httpx.Client(base_url="http://127.0.0.1:8010", trust_env=False, timeout=30) as client:
        login = client.post(
            "/auth/login",
            json={
                "user_id": "admin",
                "password": get_settings().dev_login_password.get_secret_value(),
            },
        )
        login.raise_for_status()
        client.headers["Authorization"] = "Bearer " + login.json()["access_token"]
        response = client.post(
            "/workspace/tasks",
            json={
                "intent": "Use code.execute to fix the attached repository: read mathlib.py using file_editor view, replace return a - b with return a + b using str_replace. Do not modify tests. Run the platform configured test command, finish the code agent, then call code.commit to request human approval.",
                "context": {
                    "repo_path": str(root.resolve()),
                    "declared_tools": ["code.execute", "code.commit"],
                },
            },
        )
        response.raise_for_status()
        run_id = response.json()["run_id"]
        deadline = time.monotonic() + 360
        while time.monotonic() < deadline:
            response = client.get("/runs/" + run_id)
            response.raise_for_status()
            record = response.json()
            if record["status"] not in {"queued", "running"}:
                break
            time.sleep(2)
        (root / "run.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "status": record["status"],
                    "codeplane": record.get("codeplane"),
                    "evidence": str(root),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        assert record["status"] == "awaiting_approval", record.get("errors")
        assert "a + b" in json.dumps(record.get("codeplane"))
        assert (root / "mathlib.py").read_text() == "def add(a, b):\n    return a - b\n"
        approved = client.post(
            "/codeplane/runs/" + run_id + "/approve",
            json={"note": "验收样例：批准经真实测试的修复"},
        )
        approved.raise_for_status()
        applied_id = approved.json()["run_id"]
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            applied_response = client.get("/runs/" + applied_id)
            applied_response.raise_for_status()
            applied = applied_response.json()
            if applied["status"] not in {"queued", "running"}:
                break
            time.sleep(1)
        (root / "applied.json").write_text(
            json.dumps(applied, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        assert applied.get("codeplane", {}).get("committed") is True, applied.get("codeplane")
        assert applied["status"] == "completed"
        print(
            json.dumps(
                {"approved_run": approved.json(), "original_preserved": True}, ensure_ascii=False
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
