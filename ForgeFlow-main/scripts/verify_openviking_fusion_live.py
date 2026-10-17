"""True OV server integration proof; no mocked retrieval or extraction."""

import json
import time
from pathlib import Path
from uuid import uuid4

import httpx
from verify_instruction_skill_live import wait

from forgeflow.config import get_settings


def main():
    marker = "VF-" + uuid4().hex[:10]
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
                    "viking-fusion-probe.txt",
                    f"项目名称：北辰融合验证项目\n唯一验收编号：{marker}\n".encode(),
                )
            },
        )
        uploaded.raise_for_status()
        resource = uploaded.json()
        job = None
        for _ in range(360):
            state = client.get("/context/openviking")
            state.raise_for_status()
            job = next((j for j in state.json()["jobs"] if j["id"] == resource["id"]), None)
            if job and job["status"] == "done":
                break
            time.sleep(1)
        assert job and job["status"] == "done", job
        response = client.post(
            "/workspace/tasks",
            json={
                "intent": "根据知识库资料，北辰融合验证项目的唯一验收编号是什么？只回答编号，不要猜测。"
            },
        )
        response.raise_for_status()
        run = wait(client, response.json()["run_id"])
        Path("_audit/openviking-fusion-live.json").write_text(
            json.dumps(
                {"resource": resource, "job": job, "run": run}, ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        assert marker in run["llm"]["final_answer"]["text"], run["llm"]
        assert any(s["source"] == "openviking" for s in run["llm"]["context_sources"])
        print(
            json.dumps(
                {"run_id": run["run_id"], "real_remote_index_used": True, "marker_correct": True}
            ),
            flush=True,
        )
        for _ in range(360):
            state = client.get("/context/openviking").json()
            learning = next((j for j in state["jobs"] if j["id"] == run["run_id"]), None)
            if learning and learning["status"] == "done":
                Path("_audit/openviking-learning-live.json").write_text(
                    json.dumps(learning, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(
                    json.dumps({"real_session_commit": True, "learning": learning["result"]}),
                    flush=True,
                )
                return
            time.sleep(1)
        raise AssertionError(learning)


if __name__ == "__main__":
    main()
