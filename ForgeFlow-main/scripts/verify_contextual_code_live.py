"""Verify scope-aware edits on C source uploaded as TXT with the real model."""

import json
from pathlib import Path
from urllib.parse import quote

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
        uploaded = client.post(
            "/resources/files",
            files={
                "file": (
                    "contextual-main.txt",
                    b'#include<stdio.h>\nmain()\n{\n printf("hello word");\n}\n',
                )
            },
        )
        uploaded.raise_for_status()
        response = client.post(
            "/workspace/tasks",
            json={
                "intent": "在该代码中增加一个返回语句",
                "context": {"resources": [uploaded.json()["id"]]},
            },
        )
        response.raise_for_status()
        run = wait(client, response.json()["run_id"])
        Path("_audit/contextual-code-live.json").write_text(
            json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        assert run["status"] == "completed" and run["outcome"] == "success", run.get("errors")
        artifact = next(a for a in run["artifacts"] if a.get("format") == "text")
        response = client.get(f"/runs/{run['run_id']}/artifacts/{quote(artifact['id'], safe='')}")
        response.raise_for_status()
        content = response.content.decode("utf-8-sig")
        assert (
            "return 0;" in content
            and "}" in content
            and content.index("return 0;") < content.rindex("}")
            and 'printf("hello word")' in content
        ), repr(content)
        Path("_audit/contextual-main-result.txt").write_text(content, encoding="utf-8")
        print(
            json.dumps(
                {
                    "run_id": run["run_id"],
                    "return_inside_function": True,
                    "existing_logic_preserved": True,
                }
            )
        )


if __name__ == "__main__":
    main()
