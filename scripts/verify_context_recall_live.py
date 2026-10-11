"""Real local memory -> context builder -> Ollama answer and provenance."""

import json
from pathlib import Path
from uuid import uuid4

import httpx
from verify_instruction_skill_live import wait

from forgeflow.config import get_settings


def main():
    marker = "OV-" + uuid4().hex[:10]
    with httpx.Client(base_url="http://127.0.0.1:8010", trust_env=False, timeout=90) as client:
        response = client.post(
            "/auth/login",
            json={
                "user_id": "admin",
                "password": get_settings().dev_login_password.get_secret_value(),
            },
        )
        response.raise_for_status()
        client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
        response = client.post(
            "/memory",
            json={
                "scope": "user",
                "content": f"银河项目的验收编号为 {marker}。这是已确认的项目事实。",
            },
        )
        response.raise_for_status()
        memory = response.json()
        response = client.post(
            "/workspace/tasks",
            json={"intent": "查询已有记忆：银河项目的验收编号是什么？请只回答编号。"},
        )
        response.raise_for_status()
        run = wait(client, response.json()["run_id"])
        assert run["status"] == "completed"
        assert marker in run["llm"]["final_answer"]["text"]
        assert any(s["ref_id"] == memory["id"] for s in run["llm"]["context_sources"])
        Path("_audit/openviking-local-context-live.json").write_text(
            json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {"run_id": run["run_id"], "real_memory_used": True, "provenance_recorded": True}
            )
        )


if __name__ == "__main__":
    main()
