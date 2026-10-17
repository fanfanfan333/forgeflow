"""Actual API batch smoke checks; synthetic inputs never become release holdouts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import sys
import uuid
from pathlib import Path
from urllib.parse import quote

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from verify_complex_tasks_live import fixtures, verify
from verify_recovery_live import wait


async def main():
    from forgeflow.auth.passwords import hash_password
    from forgeflow.auth.users import upsert_local_user
    from forgeflow.database import close_pool, init_pool
    from forgeflow.tenant_scope import tenant_scope

    tenant = "qa-batch-" + uuid.uuid4().hex
    password = secrets.token_urlsafe(32)
    pool = await init_pool()
    with tenant_scope(tenant):
        user = await upsert_local_user(
            pool, tenant, hash_password(password), "manager", tenant_id=tenant
        )
    output = Path("docs/quality/evidence/2026-10-16/batch-tasks")
    output.mkdir(parents=True, exist_ok=True)
    code, word, excel = fixtures()
    groups = [
        (
            "two-code-files",
            [
                ("first.txt", code[1], "code"),
                ("second.txt", code[1].replace(b"return 7", b"return 9"), "code-second"),
            ],
            "逐个修改这两份代码文件：将 hello word 修正为 hello world，在各自 main 函数内增加 return 0;，保留各自 helper 函数的原始返回值，返回两份修改后的完整文件。",
        ),
        (
            "office-batch",
            [(word[0], word[1], "word"), (excel[0], excel[1], "excel")],
            "逐个清洗全部两份文件的文字多余空格，保留 Word 表格和姓名日期金额，保留 Excel 全部工作表、数字、公式及数字格式，返回两份修改后的原格式文件。",
        ),
    ]
    results = []
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
            for label, inputs, intent in groups:
                row = {"case": label, "passed": False}
                try:
                    ids = []
                    for name, data, _kind in inputs:
                        response = await client.post(
                            "/resources/files", files={"file": (name, data)}
                        )
                        response.raise_for_status()
                        ids.append(response.json()["id"])
                    admitted = await client.post(
                        "/workspace/tasks", json={"intent": intent, "context": {"resources": ids}}
                    )
                    admitted.raise_for_status()
                    row["run_id"] = admitted.json()["run_id"]
                    run = await wait(client, row["run_id"])
                    downloads = []
                    for artifact in run.get("artifacts", []):
                        if (
                            not artifact.get("content_ref")
                            or artifact.get("kind") == "report_markdown"
                        ):
                            continue
                        response = await client.get(
                            "/runs/"
                            + row["run_id"]
                            + "/artifacts/"
                            + quote(artifact["id"], safe="")
                        )
                        response.raise_for_status()
                        downloads.append(response.content)
                        (
                            output
                            / (
                                label
                                + "-"
                                + str(len(downloads))
                                + Path(artifact.get("filename", ".bin")).suffix
                            )
                        ).write_bytes(response.content)
                    checks = {}
                    for _name, _original, kind in inputs:
                        matched = False
                        for data in downloads:
                            try:
                                candidate = (
                                    data.replace(b"return 9", b"return 7")
                                    if kind == "code-second"
                                    else data
                                )
                                valid = all(
                                    value is True
                                    for value in verify(
                                        "code" if kind == "code-second" else kind, candidate
                                    ).values()
                                )
                                if kind.startswith("code"):
                                    valid = valid and (
                                        b"return 9" in data
                                        if kind == "code-second"
                                        else b"return 7" in data
                                    )
                                matched |= valid
                            except Exception:
                                continue
                        checks[kind] = matched
                    row.update(
                        status=run["status"],
                        outcome=run.get("outcome"),
                        errors=run.get("errors"),
                        checks=checks,
                        artifact_hashes=[hashlib.sha256(data).hexdigest() for data in downloads],
                        acceptance=(run.get("llm") or {}).get("acceptance"),
                    )
                    row["passed"] = (
                        run["status"] == "completed"
                        and run.get("outcome") == "success"
                        and all(checks.values())
                        and len(downloads) >= 2
                    )
                except Exception as exc:
                    if row.get("run_id"):
                        await client.post("/runs/" + row["run_id"] + "/abort")
                    row["error"] = str(exc)[:400]
                results.append(row)
                (output / "results.json").write_text(
                    json.dumps(
                        {"provenance": "synthetic_api_smoke", "cases": results},
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        with tenant_scope(tenant):
            async with pool.acquire() as conn:
                await conn.execute("UPDATE auth_users SET disabled=TRUE WHERE id=$1", user["id"])
        await close_pool()
    return 0 if len(results) == 2 and all(row["passed"] for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
