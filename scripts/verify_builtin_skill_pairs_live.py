"""Actual paired built-in Skill smoke trials; synthetic inputs never authorize publication."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from professional_benchmark_cases import cases


def trial_cases():
    manifest = {case["id"]: case for case in cases()}
    selected = {
        "compound-code-repair": [manifest[key] for key in ("code-compound", "code-crlf")],
        "document-cleaning": [manifest[key] for key in ("document-word-0", "document-excel-0")],
        "citation-research": [manifest[key] for key in ("research-json", "research-pathlib")],
    }
    selected["repository-diagnosis"] = [
        {
            "id": "navigation-" + name,
            "files": [(name + ".py", body.encode())],
            "intent": "只读检查该 Python 文件，使用 code.lint 的 symbol 定位 parse_total 的定义和引用，不修改文件。",
            "assertions": [{"kind": "code_definition", "value": "parse_total"}],
        }
        for name, body in [
            ("function", "def parse_total(value):\n    return int(value)\nparse_total('2')\n"),
            (
                "class",
                "class Invoice:\n    def parse_total(self, value):\n        return int(value)\nInvoice().parse_total('2')\n",
            ),
        ]
    ]
    assert len(selected) == 4 and sum(map(len, selected.values())) == 8
    return selected


async def main():
    from forgeflow.database import close_pool, init_pool
    from forgeflow.evaluation.artifact_checks import check_artifacts, run_verdict
    from forgeflow.evaluation.golden_registry import GoldenCase
    from forgeflow.resources.service import get_resource_service
    from forgeflow.runtime.orchestrator import RequestContext
    from forgeflow.skills.builtin_packages import builtin_packages
    from forgeflow.skills.functional_evaluation import execute_case
    from forgeflow.skills.package_import import package_spec

    destination = Path("docs/quality/evidence/2026-10-16/builtin-skill-pairs-live.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    ctx = RequestContext(
        tenant_id="qa-skill-pairs-" + uuid.uuid4().hex,
        user_id="builtin-skill-probe",
        role="manager",
    )
    source_hash = hashlib.sha256()
    for path in sorted((Path(__file__).resolve().parents[1] / "forgeflow").rglob("*.py")):
        source_hash.update(str(path.relative_to(Path(__file__).resolve().parents[1])).encode())
        source_hash.update(path.read_bytes())
    report = {
        "runtime_hash": source_hash.hexdigest(),
        "provenance": "synthetic_smoke_not_release_holdout",
        "formal_publication_eligible": False,
        "model": "qwen3:8b",
        "cases": [],
    }
    if "--resume" in sys.argv and destination.exists():
        previous = json.loads(destination.read_text(encoding="utf-8"))
        assert previous["runtime_hash"] == report["runtime_hash"], (
            "Runtime changed; start a separate comparison"
        )
        assert (
            previous["provenance"] == report["provenance"] and previous["model"] == report["model"]
        )
        report = previous
        report["resumed_after_interruption"] = True
    completed = {(row["skill"], row["case_id"], row["variant"]) for row in report["cases"]}
    assert len(completed) == len(report["cases"]), "Duplicate trial receipts"
    packages = {item["slug"]: item for item in builtin_packages()}
    assert all(row["package_hash"] == packages[row["skill"]]["hash"] for row in report["cases"])
    await init_pool()

    def save():
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        service = get_resource_service()
        for slug, group in trial_cases().items():
            package = packages[slug]
            spec = package_spec(package["files"])
            for item in group:
                ids = []
                fingerprints = []
                for name, data in item["files"]:
                    resource = await service.register_file(
                        ctx.tenant_id, name=name, data=data, created_by=ctx.user_id
                    )
                    ids.append(resource.id)
                    fingerprints.append(hashlib.sha256(data).hexdigest())
                case = GoldenCase(
                    provenance="synthetic_smoke_not_release_holdout",
                    case_id=item["id"],
                    instruction=item["intent"],
                    source_doc_fingerprint=fingerprints[0]
                    if fingerprints
                    else hashlib.sha256(item["intent"].encode()).hexdigest(),
                    expected_summary=json.dumps(
                        {"resource_ids": ids, "assertions": item["assertions"]}
                    ),
                )
                for variant, selected in (("without_skill", None), ("with_skill", spec)):
                    if (slug, item["id"], variant) in completed:
                        continue
                    row = {
                        "skill": slug,
                        "package_hash": package["hash"],
                        "case_id": item["id"],
                        "variant": variant,
                        "passed": False,
                        "input_hashes": fingerprints,
                    }
                    started = time.monotonic()
                    try:
                        async with asyncio.timeout(300):
                            record, artifacts = await execute_case(
                                case, selected, ctx, "builtin-smoke-" + uuid.uuid4().hex
                            )
                        checks = check_artifacts(
                            artifacts, item["assertions"], record.tool_invocations
                        )
                        row.update(
                            run_id=record.run_id,
                            status=record.status,
                            outcome=record.outcome,
                            checks=checks,
                            total_tokens=record.total_tokens,
                            measured=record.total_tokens > 0
                            and record.runtime_mode == "react"
                            and not (record.llm or {}).get("degraded"),
                            budget=(record.llm or {}).get("execution_budget"),
                            errors=record.errors,
                        )
                        row["passed"] = row["measured"] and run_verdict(record, checks)
                        row["artifact_hashes"] = [
                            {"format": fmt, "sha256": hashlib.sha256(data).hexdigest()}
                            for fmt, data in artifacts
                        ]
                    except Exception as exc:
                        row["error"] = str(exc)[:400]
                    row["seconds"] = round(time.monotonic() - started, 3)
                    report["cases"].append(row)
                    save()
                    print(
                        json.dumps(
                            {key: row[key] for key in ("skill", "case_id", "variant", "passed")}
                        ),
                        flush=True,
                    )
        report["complete"] = len(report["cases"]) == 16
        report["summary"] = {
            slug: {
                variant: {
                    "passed": sum(
                        row["passed"]
                        for row in report["cases"]
                        if row["skill"] == slug and row["variant"] == variant
                    ),
                    "cases": 2,
                }
                for variant in ("without_skill", "with_skill")
            }
            for slug in packages
        }
        save()
    finally:
        await close_pool()


if __name__ == "__main__":
    os.environ.update(
        OLLAMA_MODEL="qwen3:8b",
        OLLAMA_MODEL_STRONG="qwen3:8b",
        OLLAMA_NUM_CTX="8192",
        OLLAMA_THINK="false",
        AGENT_RUNTIME_MODE="react",
    )
    asyncio.run(main())
