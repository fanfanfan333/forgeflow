"""Evaluate supervisor routing against the labelled dataset.

The dataset (``tests/data/supervisor_routing_cases.json``) is scored
per branch with a confusion matrix, following the LangGraph
supervisor-evaluation pattern used in open-source routing evals:
overall accuracy, per-class accuracy, an acceptable-route rate, and a
missed-approval rate scored separately — a required human stop that a
route skips is not averaged away into overall accuracy.

Two modes:

* ``deterministic`` (default, offline): the stage mapping documented in
  ``SupervisorAgent``'s system prompt (``forgeflow/agents/supervisor.py``):
  qualify -> researcher, research -> analyzer, analyze -> executor when
  the score is >= 4.0 else FINISH, propose -> human_approval,
  approve -> executor, done -> FINISH. This mode is a *spec-consistency*
  check: a case whose expected route disagrees with the documented
  mapping is a dataset authoring error, not a model error. It is
  therefore expected to score 100% when the dataset is consistent.
* ``llm``: the same cases are sent to a real model through Ollama's
  chat API (default model ``qwen3:8b``). The prompt is built to match
  what ``SupervisorAgent.run`` actually sends: the supervisor's own
  system prompt, the stage, a **bounded** task-intent summary (the
  case's task text truncated to 500 chars, exactly the bound
  ``SupervisorAgent.run`` applies to the last human message), the
  score and the approval status. Sending the full task text instead
  measured intent classification, not routing, and skewed the
  per-class figures (035fb2f local run: Analyzer 0/15 under the old
  full-text prompt). Scores measured with the old prompt are not
  directly comparable to scores measured with this one — the next
  local run establishes the new baseline. If Ollama is unreachable,
  the run reports "not run" and exits non-zero; it never invents a
  score.

Usage:
    python -m scripts.evaluate_supervisor_routing --mode deterministic
    python -m scripts.evaluate_supervisor_routing --mode llm \
        --model qwen3:8b --base-url http://localhost:11434 --out report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROUTES = ["researcher", "analyzer", "executor", "human_approval", "FINISH"]


def deterministic_route(case: dict[str, Any]) -> str:
    """Mirror the documented supervisor stage mapping."""
    stage = str(case.get("stage") or "")
    if stage == "qualify":
        return "researcher"
    if stage == "research":
        return "analyzer"
    if stage == "analyze":
        score = case.get("score")
        if isinstance(score, (int, float)) and score >= 4.0:
            return "executor"
        return "FINISH"
    if stage == "propose":
        return "human_approval"
    if stage == "approve":
        return "executor"
    if stage == "done":
        return "FINISH"
    return "FINISH"


def load_cases(path: Path) -> list[dict[str, Any]]:
    """Load and validate the dataset; raise on any schema violation."""
    cases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise ValueError("dataset must be a non-empty list")
    seen: set[str] = set()
    for case in cases:
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id or case_id in seen:
            raise ValueError(f"invalid or duplicate case id: {case_id!r}")
        seen.add(case_id)
        expected = case.get("expected_route")
        if expected not in ROUTES:
            raise ValueError(f"{case_id}: expected_route {expected!r} not in {ROUTES}")
        acceptable = case.get("acceptable_routes")
        if not isinstance(acceptable, list) or not acceptable:
            raise ValueError(f"{case_id}: acceptable_routes must be a non-empty list")
        for route in acceptable:
            if route not in ROUTES:
                raise ValueError(f"{case_id}: acceptable route {route!r} not in {ROUTES}")
        if not isinstance(case.get("task_text"), str) or not case["task_text"].strip():
            raise ValueError(f"{case_id}: task_text must be a non-empty string")
        if not isinstance(case.get("stage"), str) or not case["stage"]:
            raise ValueError(f"{case_id}: stage must be a non-empty string")
    return cases


def score_cases(cases: list[dict[str, Any]], predictions: dict[str, str]) -> dict[str, Any]:
    """Score predictions against expected routes."""
    per_class: dict[str, dict[str, int]] = {route: {"total": 0, "correct": 0} for route in ROUTES}
    confusion: dict[str, Counter[str]] = {route: Counter() for route in ROUTES}
    correct = 0
    acceptable_hits = 0
    missed_approval = 0
    approval_total = 0
    errors: list[dict[str, Any]] = []
    for case in cases:
        expected = str(case["expected_route"])
        predicted = str(predictions.get(case["id"], ""))
        per_class[expected]["total"] += 1
        confusion[expected][predicted] += 1
        if predicted == expected:
            correct += 1
            per_class[expected]["correct"] += 1
        if predicted == expected or predicted in case["acceptable_routes"]:
            acceptable_hits += 1
        if case.get("requires_approval"):
            approval_total += 1
            # A required stop is missed when the route skips human_approval
            # for an irreversible action: predicting executor (or anything
            # except human_approval) for a requires_approval case counts.
            if predicted != "human_approval" and expected == "human_approval":
                missed_approval += 1
        if predicted != expected:
            errors.append(
                {
                    "id": case["id"],
                    "expected": expected,
                    "predicted": predicted,
                    "acceptable": case["acceptable_routes"],
                }
            )
    total = len(cases)
    return {
        "total": total,
        "accuracy": correct / total if total else 0.0,
        "correct": correct,
        "acceptable_rate": acceptable_hits / total if total else 0.0,
        "acceptable_hits": acceptable_hits,
        "per_class_accuracy": {
            route: (stats["correct"] / stats["total"] if stats["total"] else None)
            for route, stats in per_class.items()
        },
        "per_class_total": {route: stats["total"] for route, stats in per_class.items()},
        "confusion_matrix": {route: dict(confusion[route]) for route in ROUTES},
        "missed_approval_rate": (missed_approval / approval_total if approval_total else None),
        "missed_approval": missed_approval,
        "approval_total": approval_total,
        "errors": errors,
    }


def llm_predict(case: dict[str, Any], *, model: str, base_url: str) -> str:
    """Ask Ollama for one routing decision; raise on any failure.

    The messages mirror ``SupervisorAgent.run``: its system prompt,
    then a context block with the stage, the bounded task intent
    (500 chars, the same bound the real supervisor applies), the
    score and the approval status.
    """
    import httpx

    try:
        from forgeflow.agents.supervisor import SUPERVISOR_SYSTEM
    except Exception:  # noqa: BLE001 — evaluator must stay runnable offline
        SUPERVISOR_SYSTEM = (
            "You are the Supervisor Agent for ForgeFlow. Route the workflow "
            "to one of: researcher, analyzer, executor, human_approval, FINISH."
        )
    context = (
        f"Current stage: {case['stage']}\n"
        f"Task intent: {str(case['task_text']).strip()[:500]}\n"
        f"Qualification score: {case.get('score')}\n"
        f"Approval status: {case.get('approval_status')}"
    )
    with httpx.Client(timeout=120.0) as client:
        response = client.post(
            f"{base_url.rstrip('/')}/api/chat",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": SUPERVISOR_SYSTEM},
                    {
                        "role": "user",
                        "content": f"Context:\n{context}\n\nWhat is the next routing decision? "
                        'Reply with a JSON object only: {"next": "<route>", "reasoning": "<brief reason>"}',
                    },
                ],
                "stream": False,
                "format": {
                    "type": "object",
                    "properties": {
                        "next": {"type": "string"},
                        "reasoning": {"type": "string"},
                    },
                    "required": ["next"],
                },
            },
        )
        response.raise_for_status()
        content = response.json()["message"]["content"]
    data = json.loads(content)
    route = str(data.get("next", "")).strip()
    if route not in ROUTES:
        raise ValueError(f"model returned unknown route: {route!r}")
    return route


def main(argv: list[str] | None = None) -> int:
    """Run the evaluation and print a human-readable report."""
    parser = argparse.ArgumentParser(description="Evaluate supervisor routing.")
    parser.add_argument("--mode", choices=["deterministic", "llm"], default="deterministic")
    parser.add_argument("--model", default="qwen3:8b")
    parser.add_argument("--base-url", default="http://localhost:11434")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--cases",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "tests"
        / "data"
        / "supervisor_routing_cases.json",
    )
    args = parser.parse_args(argv)
    try:
        cases = load_cases(args.cases)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"dataset error: {exc}", file=sys.stderr)
        return 2
    predictions: dict[str, str] = {}
    if args.mode == "deterministic":
        predictions = {case["id"]: deterministic_route(case) for case in cases}
    else:
        try:
            for case in cases:
                predictions[case["id"]] = llm_predict(
                    case, model=args.model, base_url=args.base_url
                )
        except Exception as exc:  # noqa: BLE001 — LLM mode must not fake a score
            print(f"llm mode not run: {exc}", file=sys.stderr)
            return 3
    report = score_cases(cases, predictions)
    report["mode"] = args.mode
    report["model"] = args.model if args.mode == "llm" else "deterministic-stage-mapping"
    lines = [
        f"mode: {report['mode']} ({report['model']})",
        f"cases: {report['total']}",
        f"accuracy: {report['accuracy']:.3f} ({report['correct']}/{report['total']})",
        f"acceptable_rate: {report['acceptable_rate']:.3f}",
        f"missed_approval_rate: {report['missed_approval_rate']}",
        "per_class_accuracy:",
    ]
    for route in ROUTES:
        lines.append(
            f"  {route}: {report['per_class_accuracy'][route]} (n={report['per_class_total'][route]})"
        )
    lines.append("confusion_matrix (expected -> predicted):")
    for route in ROUTES:
        lines.append(f"  {route}: {report['confusion_matrix'][route]}")
    if report["errors"]:
        lines.append(f"errors ({len(report['errors'])}):")
        for error in report["errors"][:20]:
            lines.append(
                f"  {error['id']}: expected={error['expected']} predicted={error['predicted']}"
            )
    print("\n".join(lines))
    if args.out is not None:
        args.out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
