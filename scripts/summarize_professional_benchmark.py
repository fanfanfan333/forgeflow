"""Compare complete pinned real-model runs without treating missing evidence as success."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import statistics
from pathlib import Path


def compare(baseline: dict, upgraded: dict) -> list[dict]:
    for field in ("dataset_hash", "model_digest", "temperature", "num_ctx", "think", "repetitions"):
        if baseline.get(field) != upgraded.get(field):
            raise ValueError("Benchmark settings differ: " + field)
    if not baseline.get("complete") or not upgraded.get("complete"):
        raise ValueError("Both benchmark phases must finish before comparison")

    def key(row):
        return row["case_id"], row["repetition"]

    if sorted(map(key, baseline["cases"])) != sorted(map(key, upgraded["cases"])):
        raise ValueError("Case manifests differ")
    rows = []
    for domain in ("code", "document", "research"):
        variants = []
        for run in (baseline, upgraded):
            cases = [case for case in run["cases"] if case["domain"] == domain]
            if len(cases) != 30:
                raise ValueError("Each domain needs ten cases repeated three times")
            variants.append(
                {
                    "tasks": len(cases),
                    "passed": sum(case["passed"] for case in cases),
                    "false_completions": sum(case.get("false_completion", False) for case in cases),
                    "tokens": sum(case.get("total_tokens") or 0 for case in cases),
                    "median_seconds": round(
                        statistics.median(case["seconds"] for case in cases), 2
                    ),
                }
            )
        old, new = variants
        rows.append(
            {
                "domain": domain,
                "baseline": old,
                "upgraded": new,
                "nonregression": new["passed"] >= old["passed"]
                and new["false_completions"] <= old["false_completions"],
            }
        )
    return rows


def calibrate_topic_case(run: dict, manifest: list[dict], folder: Path) -> dict:
    """Keep raw scores; only fold topic words the user did not request as identifiers."""
    calibrated = copy.deepcopy(run)
    calibrated["oracle_version"] = "implicit-topic-casefold-1"
    cases = {case["id"]: case for case in manifest}
    for row in calibrated["cases"]:
        if row["domain"] != "research" or row.get("outcome") != "success":
            continue
        case = cases[row["case_id"]]
        instruction = re.sub(r"https?://[^\s，。；,;]+", "", case["intent"])
        texts = []
        for artifact in row.get("artifact_hashes", []):
            path = (folder / artifact["path"].replace("\\", "/")).resolve()
            if not path.is_relative_to(folder.resolve()) or path.stat().st_size > 2_000_000:
                raise ValueError("Artifact calibration path or size is invalid")
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != artifact["sha256"]:
                raise ValueError("Artifact changed after the benchmark")
            if path.suffix in {".markdown", ".md", ".text", ".txt"}:
                texts.append(data.decode("utf-8-sig"))
        changes = []
        for index, assertion in enumerate(case["assertions"]):
            term = assertion.get("value")
            if assertion.get("kind") != "contains" or not isinstance(term, str):
                continue
            if re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", instruction, re.I):
                continue
            key = f"{index}:contains"
            folded = any(term.casefold() in text.casefold() for text in texts)
            if row.get("checks", {}).get(key) is False and folded:
                changes.append(
                    {
                        "assertion": key,
                        "term": term,
                        "reason": "topic word was not an explicitly requested identifier",
                    }
                )
                row["checks"][key] = True
        if changes:
            row["raw_passed"] = row["passed"]
            row["raw_false_completion"] = row.get("false_completion", False)
            row["calibration"] = changes
            row["passed"] = row.get("status") == "completed" and all(row["checks"].values())
            row["false_completion"] = not all(row["checks"].values())
    return calibrated


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    options = parser.parse_args()
    baseline = json.loads((options.folder / "baseline.json").read_text(encoding="utf-8"))
    upgraded = json.loads((options.folder / "final.json").read_text(encoding="utf-8"))
    rows = compare(baseline, upgraded)
    manifest = json.loads((options.folder / "cases.json").read_text(encoding="utf-8"))
    calibrated_old = calibrate_topic_case(baseline, manifest, options.folder)
    calibrated_new = calibrate_topic_case(upgraded, manifest, options.folder)
    calibrated_rows = compare(calibrated_old, calibrated_new)
    (options.folder / "baseline-calibrated.json").write_text(
        json.dumps(calibrated_old, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (options.folder / "final-calibrated.json").write_text(
        json.dumps(calibrated_new, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (options.folder / "comparison.json").write_text(
        json.dumps(
            {
                "domains": rows,
                "calibrated_domains": calibrated_rows,
                "nonregression": all(row["nonregression"] for row in rows),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    lines = [
        "# 本地模型对照评测",
        "",
        "固定模型：qwen3:8b；temperature=0；上下文8192；think=false。每类10例，各重复3次，每个版本90次。",
        "",
        "基线是已发布提交 88bf7a45aa9433ad7b1c6859172da61ef8b92ba1，升级版是报告中 source_hash 固定的本地快照。它不是本轮开始前所有未提交修改的快照，结果不能归因于某一项单独改动。",
        "",
        "| 任务 | 基线完整完成 | 升级版完整完成 | 错误完成：基线 / 升级 |",
        "|---|---:|---:|---:|",
    ]
    labels = {"code": "代码文件修改", "document": "文档与表格", "research": "官方资料研究"}
    for row in rows:
        old, new = row["baseline"], row["upgraded"]
        lines.append(
            f"| {labels[row['domain']]} | {old['passed']}/30 | {new['passed']}/30 | {old['false_completions']} / {new['false_completions']} |"
        )
    lines.extend(
        [
            "",
            "完整完成需运行状态成功且实际下载文件/来源回执满足全部断言；工具成功和最终回复不能单独算通过。",
            "",
            "研究案例在清单中显式要求 Markdown 报告。断言工具对未声明格式的额外兼容修改不影响本清单的结果。基线30个研究任务均未返回成功结果。中间失败版本及启动失败记录保留，未混入最终成绩。",
            "",
            "这些输入是独立合成回归资源，不是人工业务 Golden 保留集；代码组主要验证单文件 C 修改，不代表仓库级开发效果。耗时受网络和同时运行的离线检查影响，不作为严格速度提升证据。",
            "",
            "离线单元/数据库/浏览器回归、实际暂停恢复记录、业务 Skill 发布评测应分开核对。完整数据、实际产物哈希和模型摘要见同目录 JSON。",
        ]
    )
    lines.extend(
        [
            "",
            "## 单独列出的主题大小写校准",
            "",
            "仅对用户没有明确点名的主题词忽略大小写；代码、数据、显式技术标识符及来源核验保持严格。实际文件先核对 SHA-256，原始 JSON 和上表成绩不覆盖。此校准不计作模型能力提升。",
            "",
            "| 任务 | 校准后基线完成 | 校准后升级完成 | 校准后错误完成：基线 / 升级 |",
            "|---|---:|---:|---:|",
        ]
    )
    for row in calibrated_rows:
        old, new = row["baseline"], row["upgraded"]
        lines.append(
            f"| {labels[row['domain']]} | {old['passed']}/30 | {new['passed']}/30 | {old['false_completions']} / {new['false_completions']} |"
        )
    (options.folder / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(rows, ensure_ascii=False))


if __name__ == "__main__":
    main()
