"""Pause a real model task and resume its durable context without replaying tools."""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def wait(client, run_id):
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        response = await client.get("/runs/" + run_id)
        response.raise_for_status()
        record = response.json()
        if record["status"] not in {"running", "queued", "pending"}:
            return record
        await asyncio.sleep(0.3)
    raise TimeoutError("Task did not stop")


async def main():
    from forgeflow.auth.passwords import hash_password
    from forgeflow.auth.users import upsert_local_user
    from forgeflow.database import close_pool, init_pool
    from forgeflow.tenant_scope import tenant_scope

    tenant = "qa-recovery-" + uuid.uuid4().hex
    password = secrets.token_urlsafe(32)
    pool = await init_pool()
    with tenant_scope(tenant):
        user = await upsert_local_user(
            pool, tenant, hash_password(password), "manager", tenant_id=tenant
        )
    result = {}
    try:
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8010", trust_env=False, timeout=30
        ) as client:
            login = await client.post(
                "/auth/login",
                json={"user_id": tenant, "password": password, "workspace_id": tenant},
            )
            login.raise_for_status()
            client.headers["Authorization"] = "Bearer " + login.json()["access_token"]
            source = b'#include <stdio.h>\nint main(void) { printf("hello word"); }\n'
            upload = await client.post(
                "/resources/files", files={"file": ("resume-main.txt", source)}
            )
            upload.raise_for_status()
            admitted = await client.post(
                "/workspace/tasks",
                json={
                    "intent": "先读取文件，再将 hello word 修正为 hello world，在 main 函数内增加 return 0;，返回修改后的文件。",
                    "context": {"resources": [upload.json()["id"]]},
                },
            )
            admitted.raise_for_status()
            original = admitted.json()["run_id"]
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                checkpoint = await client.get("/runs/" + original + "/recovery")
                if checkpoint.status_code == 200 and checkpoint.json()["completed_steps"] >= 1:
                    break
                await asyncio.sleep(0.2)
            else:
                raise AssertionError("No real tool checkpoint observed")
            paused = await client.post("/runs/" + original + "/pause")
            paused.raise_for_status()
            old = await wait(client, original)
            checkpoint = await client.get("/runs/" + original + "/recovery")
            checkpoint.raise_for_status()
            frozen = checkpoint.json()
            assert frozen["can_resume"], frozen
            resumed = await client.post(
                "/runs/" + original + "/resume", json={"version": frozen["version"]}
            )
            resumed.raise_for_status()
            next_id = resumed.json()["run_id"]
            duplicate = await client.post(
                "/runs/" + original + "/resume", json={"version": frozen["version"]}
            )
            assert duplicate.status_code == 409
            new = await wait(client, next_id)
            assert new["status"] == "completed" and new["outcome"] == "success", new.get("errors")
            original_again = await client.get("/runs/" + original)
            assert original_again.json()["status"] == old["status"]
            final_checkpoint = await client.get("/runs/" + next_id + "/recovery")
            final_checkpoint.raise_for_status()
            assert (
                final_checkpoint.json()["budget"]["model_calls"] >= frozen["budget"]["model_calls"]
            )
            artifacts = [a for a in new["artifacts"] if a.get("kind") == "text_file"]
            assert artifacts
            downloaded = await client.get(
                "/runs/" + next_id + "/artifacts/" + quote(artifacts[0]["id"], safe="")
            )
            downloaded.raise_for_status()
            assert b"hello world" in downloaded.content and b"return 0;" in downloaded.content
            calls = new.get("tool_invocations", [])
            ids = [call["step_id"] for call in calls if call.get("step_id")]
            assert len(ids) == len(set(ids))
            result = {
                "original_run": original,
                "resumed_run": next_id,
                "original_status_retained": old["status"],
                "duplicate_resume_status": duplicate.status_code,
                "paused_checkpoint": frozen,
                "final_checkpoint": final_checkpoint.json(),
                "status": new["status"],
                "outcome": new["outcome"],
                "download_verified": True,
                "distinct_receipts": len(ids),
            }
            print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        with tenant_scope(tenant):
            async with pool.acquire() as conn:
                await conn.execute("UPDATE auth_users SET disabled=TRUE WHERE id=$1", user["id"])
        await close_pool()
        destination = Path("docs/quality/evidence/2026-10-15/task-recovery-live.json")
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
