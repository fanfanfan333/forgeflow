"""Finish a pinned paired run sequentially; never turn partial data into a pass."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from dotenv import dotenv_values
from summarize_professional_benchmark import compare


def write_comparison(folder: Path) -> dict:
    baseline = json.loads((folder / "baseline.json").read_text(encoding="utf-8"))
    upgraded = json.loads((folder / "upgraded.json").read_text(encoding="utf-8"))
    for run in (baseline, upgraded):
        if any(
            key not in run or run[key] is None
            for key in (
                "dataset_hash",
                "model_digest",
                "temperature",
                "num_ctx",
                "think",
                "repetitions",
                "source_hash",
            )
        ):
            raise ValueError("Benchmark metadata is incomplete; comparison refused")
    if baseline["source_hash"] == upgraded["source_hash"]:
        raise ValueError("Baseline and upgraded code snapshots are identical")
    rows = compare(baseline, upgraded)
    summary = {
        "complete": True,
        "provenance": "known_synthetic_regression",
        "non_regression": all(row["nonregression"] for row in rows),
        "domains": rows,
        "automatically_published": False,
    }
    (folder / "comparison.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# Harness real-model control",
        "",
        "Known synthetic regression: ten cases per domain, three repetitions per version. Not human business golden data.",
        "",
        "| Domain | Baseline passed | Upgraded passed | False completion: baseline / upgraded |",
        "|---|---:|---:|---:|",
    ]
    for row in rows:
        old, new = row["baseline"], row["upgraded"]
        lines.append(
            f"| {row['domain']} | {old['passed']}/30 | {new['passed']}/30 | {old['false_completions']} / {new['false_completions']} |"
        )
    lines += [
        "",
        f"Non-regression gate: {summary['non_regression']}",
        "Regressed cases: " + (", ".join(
            case for row in rows for case in row["regressed_cases"]
        ) or "none"),
        "",
        "Original assertions and raw failures retained; no post-hoc rescoring. No automatic Skill publication. Latency includes local load and is not a speed guarantee.",
    ]
    (folder / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--upgraded-root", type=Path, required=True)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8012)
    parser.add_argument("--business-revision", type=Path)
    parser.add_argument("--business-packet", type=Path)
    options = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        **{key: value for key, value in dotenv_values(root / ".env").items() if value is not None},
    }
    options.folder = options.folder.resolve()
    options.upgraded_root = options.upgraded_root.resolve()
    assert (options.upgraded_root / "forgeflow/api/main.py").is_file()
    assert (options.folder / "cases.json").is_file()
    baseline_path = options.folder / "baseline.json"

    def status(phase: str, **details):
        record = {
            "phase": phase,
            "planned_model_tasks": 180,
            "automatically_published": False,
            "measurement_complete": phase == "completed",
            "requires_review": True,
            **details,
        }
        destination = options.folder / "STATUS.json"
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(destination)

    status("waiting_for_baseline")
    deadline = time.monotonic() + 7200
    while True:
        try:
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
            if baseline.get("complete"):
                break
        except (OSError, json.JSONDecodeError):
            pass  # The runner writes a full report; never score a partial write.
        if time.monotonic() > deadline or time.time() - baseline_path.stat().st_mtime > 900:
            status("failed", reason="baseline_incomplete_or_stalled")
            raise RuntimeError("Baseline incomplete or stalled; comparison was not produced")
        time.sleep(10)
    upgraded_path = options.folder / "upgraded.json"
    if upgraded_path.exists():
        status("failed", reason="existing_upgraded_results_retained")
        raise RuntimeError("Retain existing upgraded results; do not overwrite")
    status("upgraded_running")
    result = subprocess.run(
        [
            sys.executable,
            str(options.upgraded_root / "scripts/run_professional_benchmark.py"),
            "--root",
            str(options.upgraded_root),
            "--variant",
            "upgraded",
            "--output",
            str(upgraded_path),
            "--repetitions",
            "3",
            "--port",
            str(options.port),
        ],
        cwd=options.upgraded_root,
        env=environment,
        check=False,
    )
    if result.returncode:
        status("failed", reason="upgraded_process_failed")
        raise RuntimeError("Upgraded benchmark failed; partial results retained")
    comparison = write_comparison(options.folder)
    print(json.dumps(comparison, ensure_ascii=False), flush=True)
    if options.business_revision:
        if not options.business_packet:
            raise ValueError("Business evaluation requires an approved packet")
        status("business_ablation_running", non_regression=comparison["non_regression"])
        result = subprocess.run(
            [
                sys.executable,
                str(options.upgraded_root / "scripts/run_business_skill_evaluation.py"),
                "--packet",
                str(options.business_packet),
                "--revision",
                str(options.business_revision),
                "--output",
                str(options.folder.parent / "business-ablation"),
                "--ablation",
            ],
            cwd=options.upgraded_root,
            env=environment,
            check=False,
        )
        if result.returncode:
            status(
                "failed",
                reason="business_ablation_failed",
                non_regression=comparison["non_regression"],
            )
            raise RuntimeError("Business ablation failed; original and partial evidence retained")
    status("completed", non_regression=comparison["non_regression"])


if __name__ == "__main__":
    main()
