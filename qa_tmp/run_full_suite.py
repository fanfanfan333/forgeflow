"""QA Phase 4 helper — run the full pytest suite and count from junitxml.

Why junitxml and not the summary line / exit code:
  * this box's safe-delete hook rewrites pytest's exit code to 1, and the
    "N passed" summary can be swallowed — so neither is trustworthy.
  * the junit XML carries per-testcase element names, so we can assert
    ``0 failure / 0 error / 0 skipped`` from the actual testcase nodes and
    list every non-passing case by name.

Usage:
    python qa_tmp/run_full_suite.py --profile memory   --out qa_tmp/junit_memory.xml
    python qa_tmp/run_full_suite.py --profile postgres --out qa_tmp/junit_pg.xml
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


def build_env(profile: str) -> dict:
    env = dict(os.environ)
    env["STORAGE_BACKEND"] = "memory" if profile == "memory" else "postgres"
    env["LLM_PROVIDER"] = "mock"
    env["EMBEDDING_PROVIDER"] = "mock"
    if profile == "postgres":
        env["POSTGRES_URL"] = "postgresql://forgeflow:forgeflow@127.0.0.1:5433/forgeflow"
        env["POSTGRES_SYNC_URL"] = "postgresql+psycopg://forgeflow:forgeflow@127.0.0.1:5433/forgeflow"
    return env


def parse_junit(path: Path) -> dict:
    tree = ET.parse(path)
    root = tree.getroot()
    suites = [root] if root.tag == "testsuite" else list(root)
    total = failures = errors = skipped = 0
    bad: list[dict] = []
    for suite in suites:
        if suite.tag != "testsuite":
            continue
        total += int(suite.get("tests", 0))
        failures += int(suite.get("failures", 0))
        errors += int(suite.get("errors", 0))
        skipped += int(suite.get("skipped", 0))
        for tc in suite.iter("testcase"):
            kind = None
            for child in tc:
                if child.tag in ("failure", "error", "skipped"):
                    kind = child.tag
                    break
            if kind:
                bad.append(
                    {
                        "classname": tc.get("classname"),
                        "name": tc.get("name"),
                        "type": kind,
                    }
                )
    return {
        "total": total,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
        "nonpassing": bad,
        "passed": total - failures - errors - skipped,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", choices=["memory", "postgres"], required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--tests", default="tests/")
    ap.add_argument("--extra", default="", help="extra pytest args")
    args = ap.parse_args()

    out_xml = ROOT / args.out
    out_xml.parent.mkdir(parents=True, exist_ok=True)
    cmd = [PY, "-m", "pytest", args.tests, "-q", "-p", "no:cacheprovider",
           f"--junitxml={out_xml.name if out_xml.parent == ROOT else out_xml}"]
    if args.extra:
        cmd += args.extra.split()

    env = build_env(args.profile)
    proc = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True, text=True)
    summary = {
        "profile": args.profile,
        "exit_code": proc.returncode,        # NOT trusted — see docstring
        "junit": str(out_xml),
        "stdout_tail": proc.stdout.splitlines()[-15:],
        "stderr_tail": proc.stderr.splitlines()[-15:],
    }
    if out_xml.exists():
        summary["junit_counts"] = parse_junit(out_xml)
    else:
        summary["junit_counts"] = None
        summary["error"] = "junitxml not produced"
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    counts = summary.get("junit_counts") or {}
    clean = counts.get("failures") == 0 and counts.get("errors") == 0 and counts.get("skipped") == 0
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
