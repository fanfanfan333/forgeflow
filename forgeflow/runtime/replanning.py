"""Stall-triggered replanning for the react loop (8-feature program, Phase 3).

Deliberately NOT fixed-interval planning. In a 6-round loop a periodic
planning step has weak evidence and strong cost (the research report's
comparison: smolagents ``planning_interval`` burns rounds, CrewAI caps
replans at 3, Magentic-One only replans on stall). This module
implements the Magentic-One shape, adapted to ForgeFlow:

* **Trigger:** ``STALL_THRESHOLD`` consecutive tool rounds with no
  progress. Progress is a satisfied-requirement increase OR a
  successful non-report tool invocation; anything else (all tools
  failing, blocked, refused, or only rendering) is a stall round.
* **Authority:** the framework decides, never the model. A planning
  round is a deterministic branch in the react loop: when one is due,
  the loop runs it instead of an execution model call, and a model
  halt does not skip a due planning round.
* **Budget:** planning rounds do NOT count toward the 6 execution
  rounds (``model_calls`` is untouched) and do NOT consume the shared
  execution budget (the planning call bypasses ``_ainvoke``'s budget
  accounting). The hard cap is ``MAX_PLANNING_ROUNDS`` per run
  (tracked in ``task.context["_planning_used"]`` so replan attempts
  share it — attempts reuse the same task object).
* **Degradation:** a planning failure (model error, empty parse)
  keeps the previous plan and continues execution. It never fails
  the run (CrewAI's explicit fallback rule, not smolagents' unguarded
  planning call).
* **Record type:** planning steps live in ``runtime_meta["planning_steps"]``
  (surfacing via ``record.llm`` like ``acceptance_history``). They are
  NOT tool records, so ``StuckDetector`` never sees them and cannot
  misfire on repeated plans.
* **Unchanged plan:** a replan identical to the previous plan counts
  as a no-progress signal — it does NOT reset the stall counter, so
  the loop burns the remaining planning budget and then executes
  with the old plan instead of planning forever.
* **Scope:** a plan is advisory execution order ONLY. It never
  rewrites ``requirement_list`` or the only-modify scope — those come
  from the user's original instruction, and the approval/scope checks
  keep reading the original intent, not the plan.
* **Children:** delegated children do not plan (``task.context``
  carries ``"_delegation_child"`` from ``delegation.py``). A parent
  plan snapshot is NOT yet inherited by children — that is a stated
  Phase 3 limit, not a silent behaviour.
"""

from __future__ import annotations

import re
from typing import Any

from langchain_core.messages import HumanMessage

#: Consecutive no-progress tool rounds that trigger one planning round.
STALL_THRESHOLD = 2

#: Hard cap of planning rounds per run (shared across replan attempts).
MAX_PLANNING_ROUNDS = 2

#: Successful invocations of these tools do not count as progress
#: (rendering a report is output formatting, not task advance).
PROGRESS_EXCLUDED_TOOLS = frozenset({"report.render"})

#: Marker the planning prompt carries, so tests can recognise (and
#: selectively fail) planning calls without parsing the whole prompt.
PLANNING_MARKER = "[平台重规划]"

_BULLET = re.compile(r"^\s*(?:\d+[.、)]|[-*•])\s*")
_BLOCKED = re.compile(r"^\s*受阻点\s*[:：]\s*(.*)$")


def count_satisfied(requirement_rows: list[dict[str, Any]]) -> int:
    """Rows whose assess status is ``satisfied``."""
    return sum(
        1
        for row in requirement_rows
        if isinstance(row, dict) and row.get("status") == "satisfied"
    )


def round_progressed(
    prev_satisfied: int,
    snapshot_rows: list[dict[str, Any]],
    new_records: list[dict[str, Any]],
) -> bool:
    """True when this tool round advanced the task.

    Either a requirement row newly satisfied, or at least one
    successful non-report tool invocation in the round.
    """
    if count_satisfied(snapshot_rows) > prev_satisfied:
        return True
    return any(
        isinstance(record, dict)
        and record.get("status") == "ok"
        and str(record.get("tool") or "") not in PROGRESS_EXCLUDED_TOOLS
        for record in new_records
    )


def parse_plan(text: str) -> tuple[list[str], str]:
    """Parse a planning answer into (steps, blocked note).

    Numbered/bulleted lines become steps; a ``受阻点：...`` line
    becomes the blocked note (not a step); anything else non-empty
    is kept as a step so a free-form plan still parses.
    """
    steps: list[str] = []
    blocked = ""
    for line in (text or "").splitlines():
        blocked_match = _BLOCKED.match(line)
        if blocked_match:
            blocked = blocked_match.group(1).strip()
            continue
        cleaned = _BULLET.sub("", line).strip()
        if cleaned:
            steps.append(cleaned)
    return steps, blocked


def plans_equal(a: list[str], b: list[str]) -> bool:
    """Step-list equality after whitespace normalisation."""
    norm = lambda steps: [step.strip() for step in steps if step.strip()]
    return norm(a) == norm(b)


def build_planning_prompt(
    intent: str,
    requirement_rows: list[dict[str, Any]],
    recent_records: list[dict[str, Any]],
    errors: list[str],
    previous_plan: list[str],
) -> str:
    """Prompt for one planning call (no tools bound to the call)."""
    row_lines = []
    for row in requirement_rows:
        if not isinstance(row, dict):
            continue
        row_lines.append(
            f"- {row.get('id', '?')}: {row.get('description', '')} [{row.get('status', '?')}]"
        )
    record_lines = []
    for record in recent_records[-6:]:
        if not isinstance(record, dict):
            continue
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        summary = (
            payload.get("summary")
            or str(payload.get("results", ""))[:200]
            or str(record.get("summary", ""))
        )[:200]
        record_lines.append(f"- {record.get('tool')}: {record.get('status')} — {summary}")
    previous = (
        "\n".join(f"{i + 1}. {step}" for i, step in enumerate(previous_plan))
        if previous_plan
        else "（无）"
    )
    error_lines = "\n".join(f"- {error}" for error in errors[-5:]) or "（无）"
    return (
        f"{PLANNING_MARKER}当前任务已连续多轮无进展，请给出修订后的执行计划。\n"
        f"任务：{intent}\n"
        f"需求行：\n" + "\n".join(row_lines) + "\n"
        f"最近工具结果：\n" + "\n".join(record_lines) + "\n"
        f"当前错误：\n{error_lines}\n"
        f"上一版计划：\n{previous}\n"
        "只输出计划本身：编号步骤列表，每步一句话，末尾另起一行写"
        "“受阻点：...”（无则写“无”）。不要调用工具，不要重复已完成"
        "的步骤，不要扩大任务范围（不得新增需求，不得改动原定审批"
        "与范围规则）。"
    )


def build_planning_messages(
    intent: str,
    requirement_rows: list[dict[str, Any]],
    recent_records: list[dict[str, Any]],
    errors: list[str],
    previous_plan: list[str],
) -> list[HumanMessage]:
    """One-turn planning prompt (a fresh model call, not the loop)."""
    return [
        HumanMessage(
            content=build_planning_prompt(
                intent, requirement_rows, recent_records, errors, previous_plan
            )
        )
    ]


def format_plan_message(plan: list[str], blocked: str) -> str:
    """Plan text injected into the loop as the model's own message."""
    lines = ["修订后的执行计划："]
    lines.extend(f"{i + 1}. {step}" for i, step in enumerate(plan))
    if blocked:
        lines.append(f"受阻点：{blocked}")
    return "\n".join(lines)
