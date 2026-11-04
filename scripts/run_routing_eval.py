"""Routing eval CLI (Phase 6 harness front-end).

Usage (from the repo checkout):

  # Freeze the dataset manifest (review the diff — dataset edits are
  # code changes; a run without a matching manifest fails loudly):
  python scripts/run_routing_eval.py --freeze

  # Tier A (gating): deterministic mapping over all 60 cases.
  # Exit 1 when the gate fails (accuracy != 1.0 or missed approval).
  python scripts/run_routing_eval.py --tier a

  # Tier B (alert-only): Ollama model over a seeded sample.
  # An accuracy dip beyond tolerance prints ALERT but exits 0 —
  # real-model jitter must never fail the gate.
  python scripts/run_routing_eval.py --tier b --sample-n 20 --seed 0

  # Tier B smoke (no model): deterministic predict, labeled smoke.
  python scripts/run_routing_eval.py --tier b --predict deterministic

Artifacts land in <out-dir>/ (default ./eval_runs/<run_id>/):
cases.jsonl (one line per case) + summary.json. They are run
artifacts, not repo content (eval_runs/ is gitignored).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from forgeflow.evaluation.routing_harness import (  # noqa: E402
    DEFAULT_DATASET,
    DEFAULT_MANIFEST,
    DatasetManifest,
    deterministic_predict,
    freeze_manifest,
    load_routing_cases,
    new_run_id,
    run_tier,
    sample_cases,
    verify_manifest,
    write_artifacts,
)


def _load_llm_predict():
    """Load the Ollama adapter from the sibling evaluator script.

    The script is the proven Ollama path (its tests pin it); the
    harness does not re-implement model HTTP calls.
    """
    script = Path(__file__).resolve().parent / "evaluate_supervisor_routing.py"
    spec = importlib.util.spec_from_file_location("evaluate_supervisor_routing", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load evaluator script: {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.llm_predict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Routing eval harness (routing-v1).")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--freeze", action="store_true", help="regenerate the manifest and exit")
    parser.add_argument("--tier", choices=["a", "b"], default="a")
    parser.add_argument(
        "--predict",
        choices=["deterministic", "ollama"],
        default=None,
        help="tier A default: deterministic; tier B default: ollama",
    )
    parser.add_argument("--model", default="qwen3:8b")
    parser.add_argument("--base-url", default="http://localhost:11434")
    parser.add_argument("--sample-n", type=int, default=20)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--tolerance", type=float, default=0.05)
    parser.add_argument("--baseline-accuracy", type=float, default=1.0)
    parser.add_argument("--out-dir", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.freeze:
        manifest = freeze_manifest(args.dataset)
        args.manifest.write_text(
            json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"froze {manifest.version}: {manifest.case_count} cases -> {args.manifest}")
        print(f"sha256: {manifest.sha256}")
        print(f"routes: {manifest.route_counts}")
        return 0

    if not args.manifest.exists():
        print(f"manifest not found: {args.manifest} (run --freeze first)", file=sys.stderr)
        return 2
    manifest = DatasetManifest.from_dict(json.loads(args.manifest.read_text(encoding="utf-8")))
    cases = load_routing_cases(args.dataset)
    verify_manifest(manifest, args.dataset, cases)

    predict_kind = args.predict or ("deterministic" if args.tier == "a" else "ollama")
    if args.tier == "b" and predict_kind == "deterministic":
        print("NOTE: tier B with deterministic predict is a SMOKE run, not a model measurement.")
    if predict_kind == "deterministic":
        predict = deterministic_predict
    else:
        llm_predict = _load_llm_predict()

        def predict(case):  # noqa: ANN001, ANN202
            return llm_predict(asdict(case), model=args.model, base_url=args.base_url)

    run_cases = cases if args.tier == "a" else sample_cases(cases, args.sample_n, args.seed)
    run_id = new_run_id()
    rows, report = run_tier(
        run_cases,
        predict,
        tier=args.tier,
        baseline_accuracy=args.baseline_accuracy,
        tolerance=args.tolerance,
    )
    out_dir = args.out_dir or (Path("eval_runs") / run_id)
    paths = write_artifacts(
        out_dir,
        run_id=run_id,
        dataset_version=manifest.version,
        tier=args.tier,
        rows=rows,
        report=report,
    )
    print(
        f"tier {args.tier}: accuracy={report['accuracy']:.3f} "
        f"acceptable={report['acceptable_rate']:.3f} "
        f"missed_approval={report['missed_approval_rate']} "
        f"failed={report['failed_ids']}"
    )
    print(f"cases: {paths['cases']}")
    print(f"summary: {paths['summary']}")
    if report["gate"] == "fail":
        print("GATE FAILED (tier A must be 60/60 with zero missed approvals)", file=sys.stderr)
        return 1
    if report.get("alert"):
        print(
            f"ALERT (tier B, alert-only): accuracy {report['accuracy']:.3f} below "
            f"baseline {report['baseline_accuracy']:.3f} - tolerance {report['tolerance']:.3f}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
