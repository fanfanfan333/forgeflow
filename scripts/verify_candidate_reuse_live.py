"""Check candidate reuse against the running API without publishing skills."""

import asyncio
import json
from pathlib import Path

import httpx

from forgeflow.config import get_settings


async def main():
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8010", trust_env=False, timeout=60
    ) as client:
        for attempt in range(30):
            try:
                response = await client.get("/health")
                response.raise_for_status()
                break
            except httpx.HTTPError:
                if attempt == 29:
                    raise
                await asyncio.sleep(1)
        response = await client.post(
            "/auth/login",
            json={
                "user_id": "manager-1",
                "password": get_settings().dev_login_password.get_secret_value(),
            },
        )
        response.raise_for_status()
        client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
        response = await client.get("/skill-candidates", params={"limit": 200})
        response.raise_for_status()
        before = response.json()["items"]
        source = next(
            item
            for item in before
            if item["status"] == "draft" and len(set(item["experience_ids"])) >= 3
        )
        payload = {"mode": "manual", "experience_ids": source["experience_ids"]}
        responses = await asyncio.gather(
            *[client.post("/skill-candidates", json=payload) for _ in range(3)]
        )
        for response in responses:
            response.raise_for_status()
        assert {response.json()["id"] for response in responses} == {source["id"]}
        response = await client.get("/skill-candidates", params={"limit": 200})
        response.raise_for_status()
        after = response.json()["items"]
        assert len(after) == len(before)
        response = await client.post(
            "/skill-candidates",
            json={
                "mode": "manual",
                "experience_ids": [source["experience_ids"][0]] * 10,
            },
        )
        response.raise_for_status()
        assert response.json()["status"] == "insufficient"
        proof = {
            "parallel_reuse": True,
            "before": len(before),
            "after": len(after),
            "repeated_ids_insufficient": True,
            "candidate_id": source["id"],
        }
        Path("_audit/agent-reliability-live.json").write_text(
            json.dumps(proof, indent=2), encoding="utf-8"
        )
        print(json.dumps(proof))


if __name__ == "__main__":
    asyncio.run(main())
