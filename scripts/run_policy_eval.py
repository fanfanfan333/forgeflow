"""Approval-policy eval CLI (Phase 9 B6: the harness's second dataset).

Gating tier: the default pre-approval policy must match the frozen
dataset exactly (accuracy 1.0); any miss exits 1. ``--freeze``
regenerates the manifest (a dataset change, reviewed like code).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forgeflow.evaluation.policy_harness import (  # noqa: E402
    DEFAULT_DATASET,
    DEFAULT_MANIFEST,
    PolicyManifest,
    default_predict,
    freeze_manifest,
    load_policy_cases,
    score_policy_cases,
    verify_manifest,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Approval-policy eval (frozen dataset, gating tier)"
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--freeze", action="store_true", help="regenerate the manifest and exit")
    args = parser.parse_args()

    if args.freeze:
        manifest = freeze_manifest(args.dataset)
        args.manifest.write_text(json.dumps(manifest.to_dict(), indent=2) + "\n", encoding="utf-8")
        print(
            f"froze {args.manifest} ({manifest.case_count} cases, sha256 {manifest.sha256[:12]}…) "
        )
        return 0

    manifest = PolicyManifest.from_dict(json.loads(args.manifest.read_text(encoding="utf-8")))
    cases = load_policy_cases(args.dataset)
    verify_manifest(manifest, args.dataset, cases)
    predictions = {case.id: default_predict(case.tool) for case in cases}
    result = score_policy_cases(cases, predictions)
    print(json.dumps(result, indent=2))
    return 0 if result["gate"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
