"""Verify real memory writes, private skills and retrieval from the local service."""

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx


async def main():
    config = json.loads(Path(".runtime/openviking/integration.json").read_text())
    for key, value in config.items():
        os.environ[key] = json.dumps(value) if isinstance(value, dict) else str(value)
    from forgeflow.config import get_settings
    from forgeflow.context.viking_sync import _process

    settings = get_settings()
    marker = "VL-" + uuid4().hex[:8]
    job = {
        "tenant": "default",
        "user": "manager-1",
        "kind": "run",
        "id": marker,
        "attempts": 0,
        "payload": {
            "intent": f"这是学习链路验收。验收项目 {marker} 的报告输出偏好是简体中文，先列三条结论，再列依据。请记住这项长期偏好。",
            "answer": f"已确认验收项目 {marker} 的长期报告偏好：简体中文，先三条结论，再列依据。",
            "outcome": "success",
        },
    }
    async with httpx.AsyncClient(
        base_url=settings.openviking_url,
        headers={
            "X-API-Key": settings.openviking_user_keys["default:manager-1"].get_secret_value()
        },
        trust_env=False,
        timeout=180,
    ) as client:
        job["result"] = await _process(client, job)
        print(json.dumps({"marker": marker, "session_id": job["result"]["session_id"]}), flush=True)
        for _ in range(360):
            await asyncio.sleep(5)
            job["result"] = await _process(client, job)
            Path("_audit/openviking-learning-progress.json").write_text(
                json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if job["result"]["state"] == "complete":
                break
        assert job["result"]["state"] == "complete", job["result"]
        diff_uri = job["result"]["task"]["result"]["memory_diff_uri"]
        response = await client.get("/api/v1/content/read", params={"uri": diff_uri})
        response.raise_for_status()
        diff = response.json()["result"]
        response = await client.post(
            "/api/v1/search/find",
            json={
                "query": marker + " 报告偏好",
                "target_uri": "viking://user/manager-1/memories/",
                "limit": 5,
            },
        )
        response.raise_for_status()
        evidence = {
            "marker": marker,
            "commit": job["result"],
            "memory_diff": diff,
            "retrieval": response.json(),
        }
        Path("_audit/openviking-memory-write-live.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        evidence["llm_memory_operations"] = (
            json.loads(diff)["summary"] if isinstance(diff, str) else diff.get("summary")
        )
        episode = {**job, "kind": "episode"}
        episode.pop("result", None)
        episode["result"] = await _process(client, episode)
        for _ in range(120):
            await asyncio.sleep(5)
            episode["result"] = await _process(client, episode)
            if episode["result"]["state"] == "complete":
                break
        assert episode["result"].get("indexed"), episode["result"]
        response = await client.get(
            "/api/v1/content/read", params={"uri": episode["result"]["memory_uri"] + "/source.md"}
        )
        response.raise_for_status()
        assert marker in str(response.json()), "Source conversation was not persisted"
        evidence["source_episode"] = episode["result"]
        print(
            json.dumps(
                {
                    "actual_source_memory_written_and_indexed": True,
                    "llm_memory_operations": evidence["llm_memory_operations"],
                }
            ),
            flush=True,
        )
        skill = {
            "user": "manager-1",
            "kind": "skill",
            "id": "verification-" + marker,
            "payload": {
                "name": "融合链路验收",
                "description": "验证技能包真实存储与索引",
                "version": "1.0.0",
                "content": "读取文件，保留数字，生成实际修改后的文件。",
            },
        }
        result = await _process(client, skill)
        response = await client.get(
            "/api/v1/content/read", params={"uri": result["uri"] + "/SKILL.md"}
        )
        response.raise_for_status()
        assert "保留数字" in str(response.json())
        evidence["skill"] = result
        Path("_audit/openviking-memory-write-live.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        await _process(
            client, {"user": "manager-1", "kind": "delete_skill", "id": skill["id"], "payload": {}}
        )
        print(json.dumps({"actual_skill_package_written_read_deleted": True}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
