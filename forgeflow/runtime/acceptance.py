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
    # English negations mirror the Chinese strip above: "do not edit",
    # "don't modify", "never fix" prohibit an action, so the verb inside
    # must not count as a modification request (2026-11-02 a0ab777
    # delegation parent 1ad67d4f: "This is a read-only review: do not
    # edit or execute code" gained a spurious edit requirement).
    # Bare "not" is deliberately excluded: "not only edit ... but
    # also ..." still asks for an edit.
    operation_intent = re.sub(
        r"\b(?:do not|don't|don’t|dont|cannot|can't|can’t|never)\s+(?:edit|modify|change|fix|delete|add|update|remove|clean)\b",
        "",
        operation_intent,
        flags=re.I,
    )
    operation_intent = re.sub(
        r"\b(?:without\s+(?:editing|modifying|changing|fixing|cleaning)|no\s+(?:edits|modifications|changes|fixes))\b",
        "",
        operation_intent,
        flags=re.I,
    )
    # Descriptive mentions are not modification requests. "修改后的文件",
    # "总结修改要求", "本次修改内容" describe modifications that already
    # exist; only a bare imperative ("修改文件", "请修改要求并总结")
    # asks this run to modify files. The negation strip above only
    # covered explicit "不修改", so a read-only request that merely
    # mentioned modifications gained an edit requirement it could never
    # satisfy, and the run ended partial with empty errors (2026-11-02
    # c8053bc delegation parent 5b9b0e26: research satisfied via
    # delegation, edit unverified, outcome partial). Attributive 的/后
    # can only be descriptive — an imperative never takes them — and a
    # read/summarize verb immediately before 修改要求/修改内容 makes
    # the phrase its topic, not its action.
    operation_intent = re.sub(
        r"(?:修改|修复|优化|清洗|整理|删除|增加|新增)(?:后)?的",
        "",
        operation_intent,
    )
    operation_intent = re.sub(
        r"(?:本次|这次|该|其|上述)(?:修改|修复|优化)(?:的)?(?:要求|需求|内容|记录|历史|说明|点|部分|情况|原因|日志)(?!文件|文档|代码|表格|函数)",
        "",
        operation_intent,
    )
    operation_intent = re.sub(
        r"((?:总结|概括|说明|解释|查看|读取|阅读|研究|分析|介绍|描述|了解)(?:了|一下)?(?:的|本次|这次|该|其|其中的)?)(?:修改|修复|优化)(?:要求|需求|内容|点)",
        r"\1",
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


def _delegated_research_evidence(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Evidence entries from a successful research-role delegation, if any.

    A parent that researches *through* agent.delegate never calls
    research.verify itself, yet the child could not have succeeded
    without a real read: the delegation layer fails a child with no
    reviewed tool call ("子任务没有实际读取或检查任何证据"). So a
    research-role delegate result that is ok and carries non-empty
    evidence is real research evidence for the parent's research
    requirement — otherwise delegation-only research could never
    satisfy acceptance, and the parent failed on "需求尚未全部验证"
    even though the experts read the sources (2026-11-01 6fad659
    delegation run fc059787). Cached repeats count too: the cached
    body carries the first conclusion's evidence verbatim. A failed
    research item, a non-research role, or empty evidence counts
    for nothing here.
    """
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("results"), list):
        items = payload["results"]
    elif payload.get("role"):
        items = [payload]
    else:
        return []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("role") != "research" or not item.get("ok"):
            continue
        evidence = item.get("evidence") or []
        if isinstance(evidence, list) and evidence:
            return [e for e in evidence if isinstance(e, dict)]
    return []


def assess(items: list[dict[str, Any]], invocations: list[dict[str, Any]]) -> dict[str, Any]:
    result = []
    edits = {"textfile.edit", "document.edit", "sheet.edit", "code.execute", "code.commit"}
    for item in items:
        kind = item.get("kind", item["id"])
        row = {**item, "status": "unverified", "evidence": []}
        candidates = [
            r
            for r in invocations
            if r.get("tool")
            in ({"research.verify", "agent.delegate"} if kind == "research" else edits)
        ]
        for record in candidates:
            payload = record.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            if item.get("source_path") and payload.get("file") != item["source_path"]:
                continue
            # Research evidence accumulates (2026-11-02 delegation run
            # 73d610c2): the first delegation returned two successful
            # experts, yet the parent still failed, because every
            # candidate reset the row and the LAST delegate record won
            # — a later other-role or failed delegate wiped the good
            # research evidence. A later edit superseding an earlier
            # one is correct for edits, but research evidence does not
            # un-happen, so a satisfied research row is never reset.
            if kind == "research" and row["status"] == "satisfied":
                continue
            # A later edit supersedes earlier evidence for the delivered result.
            row["status"] = "unverified"
            row["evidence"] = []
            validation = payload.get("validation") or {}
            verified = validation.get("requirement_ok") is True
            if kind == "return":
                verified = validation.get("return_ok") is True
            elif kind == "research" and record.get("tool") == "agent.delegate":
                delegated = _delegated_research_evidence(payload)
                verified = bool(delegated)
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
            elif record.get("tool") == "code.commit" and kind == "edit":
                # 2026-10-30 resume 20b6a1e0: code.commit is the terminal
                # code-plane tool, but it was not in the edits set, so
                # an approval resume that only committed+applied could
                # never satisfy its edit requirement and was always
                # partial. A successful commit (committed, non-empty
                # diff, no apply conflicts) is edit evidence; it is
                # deliberately NOT return evidence — the return value
                # is proven by the original code.execute tests, not by
                # the commit step.
                code = payload.get("codeplane") or {}
                verified = (
                    record.get("status") == "ok"
                    and bool(payload.get("committed"))
                    and bool(code.get("diff"))
                    and not (code.get("apply_conflicts") or [])
                )
            elif kind == "edit" and "return_ok" in validation:
                # Return placement alone is not evidence of the other requested edits.
                verified = validation.get("edit_ok") is True
            # A parallel batch can carry a successful research item while
            # its overall record reads error (failure isolation: one
            # child's failure does not cancel the others), so delegate
            # records are gated on the research item itself, not the
            # batch record's status.
            if (
                record.get("status") == "ok"
                or (kind == "research" and record.get("tool") == "agent.delegate")
            ) and verified:
                row["status"] = "satisfied"
                if kind == "research" and record.get("tool") == "agent.delegate":
                    row["evidence"] = [
                        {
                            "step_id": e.get("step_id"),
                            "tool": e.get("tool"),
                            "result_ref": e.get("result_ref"),
                        }
                        for e in _delegated_research_evidence(payload)
                    ]
                else:
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


def recovered_tool_errors(
    invocations: list[dict[str, Any]], *, acceptance_complete: bool = False
) -> tuple[set[str], list[str]]:
    """Resolve local-file retries or a later complete research review; retain historical receipts.

    ``acceptance_complete`` gates the delegation rule: a failed
    ``agent.delegate`` is historical ONLY once the run's own acceptance
    is complete — the deliverable then stands without that review, so
    a side delegation that read nothing (2026-11-01 B run 3ba456dc:
    code diff + pytest 3/3, but the parent failed on the delegate
    error) must not fail the whole run. Without complete acceptance
    the delegate error is retained, so a run whose deliverable depends
    on the delegation still fails honestly. Both executor message
    forms are covered: the tool-loop "未成功执行" form and the replan
    "尚未修复" form name the same record.
    """
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
    if acceptance_complete:
        for record in invocations:
            if record.get("tool") != "agent.delegate":
                continue
            if record.get("status") not in {"error", "unavailable", "refused"}:
                continue
            detail = record.get("error") or record.get("summary") or "执行失败"
            recovered.add(
                f"工具 'agent.delegate' 未成功执行：{record.get('error') or record.get('summary')}"
            )
            recovered.add(f"工具 'agent.delegate' 尚未修复：{detail}")
            if record.get("step_id"):
                steps.append(record.get("step_id"))
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
