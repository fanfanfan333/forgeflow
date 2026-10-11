"""Verify real dataset quality, arithmetic evidence and report delivery."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from verify_instruction_skill_live import wait

from forgeflow.config import get_settings


def main():
    with httpx.Client(base_url="http://127.0.0.1:8010", trust_env=False, timeout=30) as client:
        auth = client.post(
            "/auth/login",
            json={
                "user_id": "manager-1",
                "password": get_settings().dev_login_password.get_secret_value(),
            },
        )
        auth.raise_for_status()
        client.headers["Authorization"] = "Bearer " + auth.json()["access_token"]
        uploaded = client.post(
            "/resources/files",
            files={
                "file": ("data-workbench-verification.csv", b"id,amount\n1,10\n2,20\n3,\n4,40\n")
            },
        )
        uploaded.raise_for_status()
        resource = uploaded.json()
        assert resource["summary"]["rows"] == 4
        assert resource["summary"]["quality"]["empty_columns"]["amount"] == 0.25
        task = client.post(
            "/workspace/tasks",
            json={
                "intent": "分析已附加的销售CSV，使用 analysis.profile 实际统计 amount 合计，说明缺失值及统计依据，生成运行报告。",
                "context": {"resources": [resource["id"]], "column": "amount"},
            },
        )
        task.raise_for_status()
        record = wait(client, task.json()["run_id"])
        observed = [
            x
            for x in record.get("tool_invocations", [])
            if x.get("tool") == "analysis.profile" and x.get("executed")
        ]
        assert record["status"] == "completed", record.get("errors")
        assert observed and observed[0]["payload"]["aggregate_value"] == 70
        assert observed[0]["payload"]["rows"] == 4
        assert record["artifacts"]
        Path("_audit/data-workbench-live.json").write_text(
            json.dumps({"resource": resource, "run": record}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "run_id": record["run_id"],
                    "resource_id": resource["id"],
                    "rows": 4,
                    "missing_amount_ratio": 0.25,
                    "actual_total": 70,
                    "report_delivered": True,
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
