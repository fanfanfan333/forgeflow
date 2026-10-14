"""Requirement status comes from execution evidence, never the final narration."""

from __future__ import annotations

import re
from typing import Any


def requirement_list(intent: str, context: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    if re.search(r"返回(?:值)?(?:的)?语句|\breturn\b", intent, re.I):
        items.append(("return", "在目标函数内增加返回语句"))
    edit_request = re.search(
        r"优化|修复|修改|清洗|整理|删除|增加|新增|edit|fix|clean", intent, re.I
    )
    return_only = bool(items) and not re.search(
        r"优化|修复|修改|清洗|整理|删除|edit|fix|clean", intent, re.I
    )
    if (
        edit_request
        and not return_only
        and any(
            context.get(k)
            for k in (
                "resources",
                "paths",
                "repo_path",
                "document_paths",
                "text_paths",
                "sheet_paths",
            )
        )
    ):
        items.append(("edit", "按原始要求修改并校验文件"))
    if re.search(r"研究|调研|搜索|查找.*资料|research|search", intent, re.I):
        items.append(("research", "读取实际来源并提供可核对的研究证据"))
    return [
        {
            "id": key,
            "description": description,
            "original_request": intent,
            "status": "unverified",
            "evidence": [],
        }
        for key, description in items
    ]


def assess(items: list[dict[str, Any]], invocations: list[dict[str, Any]]) -> dict[str, Any]:
    result = []
    edits = {"textfile.edit", "document.edit", "sheet.edit", "code.execute"}
    for item in items:
        row = {**item, "status": "unverified", "evidence": []}
        candidates = [
            r
            for r in invocations
            if r.get("tool") in ({"research.verify"} if item["id"] == "research" else edits)
        ]
        for record in candidates:
            payload = record.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            # A later edit supersedes earlier evidence for the delivered result.
            row["status"] = "unverified"
            row["evidence"] = []
            validation = payload.get("validation") or {}
            verified = validation.get("requirement_ok") is True
            if item["id"] == "return":
                verified = validation.get("return_ok") is True
            elif item["id"] == "research":
                verified = bool(
                    payload.get("artifact_ref")
                    and validation.get("citations_ok") is True
                    and validation.get("claims_supported") is True
                )
            elif record.get("tool") == "code.execute" and item["id"] != "return":
                code = payload.get("codeplane") or {}
                tests = code.get("tests") or {}
                verified = (
                    bool(code.get("diff"))
                    and tests.get("measured") is True
                    and tests.get("verdict") == "passed"
                    and not tests.get("failed")
                )
            elif item["id"] == "edit" and "return_ok" in validation:
                # Return placement alone is not evidence of the other requested edits.
                verified = validation.get("edit_ok") is True
            if record.get("status") == "ok" and verified:
                row["status"] = "satisfied"
                row["evidence"].append(
                    {
                        "step_id": record.get("step_id"),
                        "tool": record.get("tool"),
                        "result_ref": record.get("result_ref"),
                    }
                )
            elif row["status"] != "satisfied" and (
                validation.get("requirement_ok") is False
                or (item["id"] == "research" and validation.get("claims_supported") is False)
                or (item["id"] == "return" and validation.get("return_ok") is False)
                or record.get("status") in {"error", "refused", "unavailable"}
            ):
                row["status"] = "unsatisfied"
        result.append(row)
    return {
        "requirements": result,
        "complete": bool(result) and all(r["status"] == "satisfied" for r in result),
        "verification": "measured" if result else "not_required",
    }
