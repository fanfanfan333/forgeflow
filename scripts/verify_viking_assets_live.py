"""Verify indexed source memory, native Agent recall and private skill CRUD."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import httpx
from verify_instruction_skill_live import wait

from forgeflow.config import get_settings
from forgeflow.context.viking_sync import _process


async def assets():
    config = json.loads(Path(".runtime/openviking/integration.json").read_text(encoding="utf-8"))
    episode = json.loads(Path("_audit/openviking-episode-probe.json").read_text(encoding="utf-8"))
    async with httpx.AsyncClient(
        base_url=config["OPENVIKING_URL"],
        headers={"X-API-Key": config["OPENVIKING_USER_KEYS"]["default:manager-1"]},
        trust_env=False,
        timeout=120,
    ) as client:
        episode.pop("result", None)
        episode["result"] = await _process(client, episode)
        for _ in range(120):
            await asyncio.sleep(5)
            episode["result"] = await _process(client, episode)
            if episode["result"]["state"] == "complete":
                break
        result = episode["result"]
        assert result["indexed"], result
        response = await client.get(
            "/api/v1/content/read", params={"uri": result["memory_uri"] + "/source.md"}
        )
        response.raise_for_status()
        assert episode["id"] in str(response.json())
        evidence = {"source_episode": result, "source_read_verified": True, "marker": episode["id"]}
        skill = {
            "user": "manager-1",
            "kind": "skill",
            "id": "asset-probe-" + uuid4().hex,
            "payload": {
                "name": "融合技能验收",
                "description": "真实包读写更新删除验收",
                "version": "1.0.0",
                "content": "读取文件，保留数字，再生成实际修改后的文件。",
            },
        }
        skill["result"] = await _process(client, skill)
        for _ in range(120):
            if not skill["result"].get("task_id"):
                break
            await asyncio.sleep(5)
            skill["result"] = await _process(client, skill)
            if skill["result"]["state"] in {"complete", "available"}:
                break
        assert skill["result"]["state"] in {"complete", "available"}, skill["result"]
        response = await client.get(
            "/api/v1/content/read", params={"uri": skill["result"]["uri"] + "/SKILL.md"}
        )
        response.raise_for_status()
        assert "保留数字" in str(response.json())
        evidence["skill_initial"] = skill["result"]
        skill.pop("result", None)
        skill["payload"]["version"] = "1.0.1"
        skill["payload"]["content"] = "读取文件，保留数字和公式，生成实际修改后的文件。"
        updated = await _process(client, skill)
        response = await client.get(
            "/api/v1/content/read", params={"uri": updated["uri"] + "/SKILL.md"}
        )
        response.raise_for_status()
        assert "Version: 1.0.1" in str(response.json()) and "公式" in str(response.json())
        evidence["skill_updated"] = updated
        deletion = {
            "user": "manager-1",
            "kind": "delete_skill",
            "id": skill["id"],
            "payload": {"task_ids": [updated["task_id"]] if updated.get("task_id") else []},
        }
        for _ in range(60):
            try:
                removed = await _process(client, deletion)
                if removed["state"] == "deleted":
                    break
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 409:
                    raise
            await asyncio.sleep(2)
        else:
            raise AssertionError("Skill deletion did not settle")
        response = await client.get(
            "/api/v1/content/read", params={"uri": updated["uri"] + "/SKILL.md"}
        )
        assert response.status_code == 404
        evidence["skill_deleted_verified"] = True
        Path("_audit/openviking-assets-live.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {"source_memory_indexed_read": True, "skill_package_read_updated_deleted": True}
            ),
            flush=True,
        )
    return episode["id"]


def main():
    marker = asyncio.run(assets())
    with httpx.Client(base_url="http://127.0.0.1:8010", trust_env=False, timeout=120) as client:
        login = client.post(
            "/auth/login",
            json={
                "user_id": "manager-1",
                "password": get_settings().dev_login_password.get_secret_value(),
            },
        )
        login.raise_for_status()
        client.headers["Authorization"] = "Bearer " + login.json()["access_token"]
        response = client.post(
            "/workspace/tasks",
            json={
                "intent": f"根据历史资料，验收项目 {marker} 的报告输出偏好是什么？只根据记录回答。"
            },
        )
        response.raise_for_status()
        run = wait(client, response.json()["run_id"], timeout=360)
        Path("_audit/openviking-agent-memory-recall-live.json").write_text(
            json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        answer = run["llm"]["final_answer"]["text"]
        assert "中文" in answer and "结论" in answer, answer
        assert any(
            source["source"] == "openviking" and "/memories/" in source["ref_id"]
            for source in run["llm"]["context_sources"]
        ), run["llm"]
        print(json.dumps({"agent_memory_recall": True, "run_id": run["run_id"]}), flush=True)


if __name__ == "__main__":
    main()
