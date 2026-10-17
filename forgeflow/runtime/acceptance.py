"""Requirement status comes from execution evidence, never the final narration."""

from __future__ import annotations

import re
from typing import Any


def pending_batch_cleanup(intent: str, requirements: list[dict], records: list[dict], tools: Any) -> list[dict]:
    """Finish explicit Office whitespace batches through the normal tool boundary."""
    from forgeflow.documents.whitespace import whitespace_only_request

    targets = [item for item in requirements if item.get("kind") == "edit" and item.get("source_path")]
    direct_request = re.match(r"^(?:(?:请|帮我|请帮我|逐个|逐一|批量)\s*)*(?:清洗|清理|整理|规范|去掉|去除)|^(?:please\s+)?(?:clean|trim|normalize)\b", intent.strip(), re.I)
    if not direct_request or len(targets) < 2 or not whitespace_only_request(intent) or re.search(
        r"(?:不要|不得|禁止|无需|不需要|不)(?:进行|再)?(?:清洗|清理|整理|规范|去掉|去除|修改|编辑)|do not|don't",
        intent, re.I,
    ) or re.search(r"只读|是否|能否|为什么|为何|如何|怎么|解释|介绍|\b(?:explain|describe|why|how)\b", intent, re.I):
        return []
    calls = []
    for item in targets:
        parts = item["id"].split(":")
        tool = {"document": "document.edit", "sheet": "sheet.edit"}.get(parts[1])
        if tool not in tools or any(
            record.get("tool") == tool and (
                (record.get("executed") is not False and (
                    not isinstance(record.get("payload"), dict) or not record["payload"].get("file")
                )) or (isinstance(record.get("payload"), dict) and record["payload"].get("file") == item["source_path"])
            ) for record in records
        ):
            continue
        calls.append({"name": tool, "id": "platform-cleanup-" + item["id"],
                      "args": {"file_index": int(parts[2]), "edits": [{"op": "normalize_whitespace"}]}})
    return calls


def requirement_list(
    intent: str, context: dict[str, Any], inputs: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    items = []
    if re.search(r"返回(?:值)?(?:的)?语句|\breturn\b", intent, re.I):
        items.append(("return", "在目标函数内增加返回语句"))
    operation_intent = re.sub(r"返回(?:修改|处理|修复|优化)?后(?:的)?(?:完整)?文件", "", intent)
    operation_intent = re.sub(
        r"(?:不要|不得|禁止|无需|不需要|不)(?:进行|再)?(?:修改|编辑|删除|增加|新增|替换)",
        "",
        operation_intent,
    )
    edit_request = re.search(
        r"优化|修复|修正|纠正|更正|修改|替换|清洗|整理|删除|增加|新增|edit|fix|clean",
        operation_intent,
        re.I,
    )
    return_only = bool(items) and not re.search(
        r"优化|修复|修正|纠正|更正|修改|替换|清洗|整理|删除|edit|fix|clean", operation_intent, re.I
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
    rows = [
        {
            "id": key,
            "description": description,
            "original_request": intent,
            "status": "unverified",
            "evidence": [],
        }
        for key, description in items
    ]

    targets = []
    for plane in ("text", "document", "sheet", "pdf"):
        paths = (inputs or {}).get(plane + "_paths") or []
        names = (inputs or {}).get(plane + "_names") or []
        if not isinstance(paths, (list, tuple)):
            continue
        for index, path in enumerate(paths):
            if isinstance(path, str) and path not in {item[2] for item in targets}:
                name = (
                    names[index]
                    if isinstance(names, (list, tuple)) and index < len(names)
                    else f"文件 {index + 1}"
                )
                targets.append((plane, index, path, name))
    if len(targets) > 1 and re.search(
        r"全部|所有|每个|逐个|逐一|批量|多个文件|多份|这[两三]份|batch|all files", intent, re.I
    ):
        expanded = []
        for row in rows:
            selected = [
                target
                for target in targets
                if row["id"] == "edit" or (row["id"] == "return" and target[0] == "text")
            ]
            if row["id"] not in {"edit", "return"} or len(selected) < 2:
                expanded.append(row)
                continue
            expanded.extend(
                {
                    **row,
                    "id": f"{row['id']}:{plane}:{index}",
                    "kind": row["id"],
                    "source_path": path,
                    "description": f"处理并校验：{name}",
                }
                for plane, index, path, name in selected
            )
        rows = expanded
    return rows


def assess(items: list[dict[str, Any]], invocations: list[dict[str, Any]]) -> dict[str, Any]:
    result = []
    edits = {"textfile.edit", "document.edit", "sheet.edit", "code.execute"}
    for item in items:
        kind = item.get("kind", item["id"])
        row = {**item, "status": "unverified", "evidence": []}
        candidates = [
            r
            for r in invocations
            if r.get("tool") in ({"research.verify"} if kind == "research" else edits)
        ]
        for record in candidates:
            payload = record.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            if item.get("source_path") and payload.get("file") != item["source_path"]:
                continue
            # A later edit supersedes earlier evidence for the delivered result.
            row["status"] = "unverified"
            row["evidence"] = []
            validation = payload.get("validation") or {}
            verified = validation.get("requirement_ok") is True
            if kind == "return":
                verified = validation.get("return_ok") is True
            elif kind == "research":
                verified = bool(
                    payload.get("artifact_ref")
                    and validation.get("citations_ok") is True
                    and validation.get("claims_supported") is True
                )
            elif record.get("tool") == "code.execute" and kind != "return":
                code = payload.get("codeplane") or {}
                tests = code.get("tests") or {}
                verified = (
                    bool(code.get("diff"))
                    and tests.get("measured") is True
                    and tests.get("verdict") == "passed"
                    and not tests.get("failed")
                )
            elif kind == "edit" and "return_ok" in validation:
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
                or (kind == "research" and validation.get("claims_supported") is False)
                or (kind == "return" and validation.get("return_ok") is False)
                or record.get("status") in {"error", "refused", "unavailable"}
            ):
                row["status"] = "unsatisfied"
        result.append(row)
    return {
        "requirements": result,
        "complete": bool(result) and all(r["status"] == "satisfied" for r in result),
        "verification": "measured" if result else "not_required",
    }


def recovered_tool_errors(invocations: list[dict[str, Any]]) -> tuple[set[str], list[str]]:
    """Resolve local-file retries or a later complete research review; retain historical receipts."""
    latest = {}
    recovered, unresolved, steps = set(), set(), []
    for record in reversed(invocations):
        if record.get("tool") not in {"textfile.edit", "document.edit", "sheet.edit"}:
            continue
        payload = record.get("payload") or {}
        if not isinstance(payload, dict) or not payload.get("file"):
            continue
        key = (record["tool"], payload["file"])
        if key not in latest:
            latest[key] = (
                record.get("status") == "ok"
                and bool(payload.get("artifact_ref"))
                and (payload.get("validation") or {}).get("requirement_ok") is True
            )
        if record.get("status") == "error":
            message = f"工具 '{record['tool']}' 未成功执行：{record.get('error') or record.get('summary')}"
            if latest[key]:
                recovered.add(message)
                steps.append(record.get("step_id"))
            else:
                unresolved.add(message)
    research = [record for record in invocations if record.get("tool") == "research.verify"]
    if research:
        last = research[-1]
        payload = last.get("payload") or {}
        validation = payload.get("validation") or {} if isinstance(payload, dict) else {}
        if (
            last.get("status") == "ok"
            and isinstance(payload, dict)
            and payload.get("artifact_ref")
            and all(
                validation.get(key) is True
                for key in ("citations_ok", "claims_supported", "coverage_ok")
            )
        ):
            for record in research[:-1]:
                if record.get("status") == "error":
                    recovered.add(
                        f"工具 'research.verify' 未成功执行：{record.get('error') or record.get('summary')}"
                    )
                    steps.append(record.get("step_id"))
    return recovered - unresolved, steps
