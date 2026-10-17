"""Command-line entry point: ``python -m forgeflow.qa`` (§59).

Subcommands: ``discover | plan | run | analyze | report | full``. The exit code is
CI-friendly: ``0`` unless the run's verdict is ``FAIL`` or ``BLOCKED`` (which
return ``1``), and ``2`` on a usage/programming error.

The CLI is a *new* entry point (``forgeflow.qa``), deliberately separate from
``forgeflow.bootstrap`` — the bootstrap module is a MANAGED file under the lint /
typecheck gates, and adding a subcommand there is a later increment (§9 item 10).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import cast

from forgeflow.qa.config import DEFAULT_TIMEOUT_S, Profile, QaRunConfig
from forgeflow.qa.models import Verdict
from forgeflow.qa.orchestrator import QAOrchestrator, Stage
from forgeflow.qa.paths import QaPaths

logger = logging.getLogger("forgeflow.qa")

_STAGES: tuple[str, ...] = ("discover", "plan", "run", "analyze", "report", "full")

#: Stages whose verdict determines the process exit code.
_VERDICT_STAGES = frozenset({"full", "report"})

#: Verdicts that must fail CI.
_FAILING_VERDICTS = frozenset({Verdict.FAIL.value, Verdict.BLOCKED.value})


def build_parser() -> argparse.ArgumentParser:
    """Build the ``forgeflow.qa`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="forgeflow.qa",
        description=(
            "ForgeFlow QA Code Agent (Phase A–C): discover → plan → run → analyze → report."
        ),
    )
    parser.add_argument(
        "stage",
        nargs="?",
        default="full",
        choices=list(_STAGES),
        help="Which stage to run (default: full).",
    )
    parser.add_argument(
        "--profile",
        choices=[Profile.FAST.value, Profile.FULL.value],
        default=Profile.FAST.value,
        help="Run profile (default: fast).",
    )
    parser.add_argument(
        "--root",
        default=None,
        help="Project root (default: auto-detected from the package location).",
    )
    parser.add_argument(
        "--artifacts-dir",
        dest="artifacts_dir",
        default=None,
        help="Explicit run directory (default: <tmp_root>/qa/<run_id>).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_S,
        help=f"Per-subprocess timeout in seconds (default: {DEFAULT_TIMEOUT_S}).",
    )
    parser.add_argument(
        "--run-id",
        dest="run_id",
        default=None,
        help="Explicit run id (default: a fresh timestamped id).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit a machine-readable JSON result.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the requested stage; return the process exit code."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )
    parser = build_parser()
    args = parser.parse_args(argv)

    run_id = args.run_id or QaPaths.new_run_id()
    artifacts_dir: Path | None = Path(args.artifacts_dir) if args.artifacts_dir else None

    try:
        paths = QaPaths.resolve(Path(args.root) if args.root else None)
        if artifacts_dir is None:
            artifacts_dir = paths.run_dir(run_id)
        config = QaRunConfig(
            project_root=paths.project_root,
            artifacts_dir=artifacts_dir,
            run_id=run_id,
            profile=Profile(args.profile),
            timeout_s=args.timeout,
        )
        orchestrator = QAOrchestrator(config)
        result = orchestrator.run_stage(cast(Stage, args.stage))
    except Exception as exc:  # noqa: BLE001 - a CLI must report, not traceback
        logger.error("QA run failed: %s", exc)
        _emit(
            args.as_json,
            {
                "status": "ERROR",
                "run_id": run_id,
                "artifacts_dir": str(artifacts_dir),
                "stage": args.stage,
                "error": f"{type(exc).__name__}: {exc}",
            },
            stderr=True,
        )
        return 2

    verdict = None
    final_summary = orchestrator.state.get("final_summary")
    if isinstance(final_summary, dict):
        verdict = final_summary.get("verdict")

    _emit(
        args.as_json,
        {
            "status": "OK",
            "run_id": run_id,
            "artifacts_dir": str(artifacts_dir),
            "stage": args.stage,
            "result": str(result) if result is not None else None,
            "verdict": verdict,
        },
        stderr=False,
    )

    if args.stage in _VERDICT_STAGES and verdict in _FAILING_VERDICTS:
        return 1
    return 0


def _emit(as_json: bool, payload: dict, *, stderr: bool) -> None:
    """Print a JSON or human-readable result to stdout/stderr."""
    if as_json:
        text = json.dumps(payload, indent=2, ensure_ascii=False)
    else:
        parts = [f"{key}={value}" for key, value in payload.items() if key != "status"]
        text = " ".join(parts)
    stream = sys.stderr if stderr else sys.stdout
    print(text, file=stream)


__all__ = ["build_parser", "main"]
