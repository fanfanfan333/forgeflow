"""Verify natural-language draft -> real trial -> saved skill -> evidence."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import httpx
from verify_instruction_skill_live import wait

from forgeflow.config import get_settings


def main():
    with httpx.Client(base_url="http://127.0.0.1:8010", trust_env=False, timeout=120) as client:
        response = client.post(
            "/auth/login",
            json={
                "user_id": "manager-1",
                "password": get_settings().dev_login_password.get_secret_value(),
            },
        )
        response.raise_for_status()
        client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
        response = client.post(
            "/skills/instructions/draft",
            json={
                "goal": "学会清洗用户上传的文本、Word和Excel文件，清理多余空格，保留姓名、金额、数字和公式，返回实际修改后的文件，并核对内容。"
            },
        )
        response.raise_for_status()
        skill = response.json()
        skill["name"] = "指令清洗验收-" + uuid4().hex[:6]
        uploaded = client.post(
            "/resources/files",
            files={"file": ("studio-trial.txt", "客户：  张三\n金额：  100万元\n".encode())},
        )
        uploaded.raise_for_status()
        tried = client.post(
            "/skills/instructions/try",
            json={
                "skill": skill,
                "resources": [uploaded.json()["id"]],
                "intent": "清理这份文本的连续多余空格，保留张三和100万元，保存并返回文件。",
            },
        )
        tried.raise_for_status()
        run = wait(client, tried.json()["run_id"])
        assert run["status"] == "completed" and run["outcome"] == "success", run.get("errors")
        artifact = next(a for a in run["artifacts"] if a.get("format") == "text")
        content = client.get(f"/runs/{run['run_id']}/artifacts/{artifact['id']}")
        content.raise_for_status()
        text = content.content.decode("utf-8-sig")
        assert "张三" in text and "100万元" in text and "  " not in text
        skill["trial_run_id"] = run["run_id"]
        saved = client.post("/skills/instructions", json=skill)
        saved.raise_for_status()
        Path("_audit/skill-studio-live.json").write_text(
            json.dumps(
                {"draft": skill, "trial": run, "skill": saved.json()}, ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "draft_generated": True,
                    "trial_passed": True,
                    "actual_file_changed": True,
                    "run_id": run["run_id"],
                    "skill_id": saved.json()["id"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
