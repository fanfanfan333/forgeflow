"""The single source of truth for the platform context block (INC29 T04 review D2).

ForgeFlow is the **control plane** and owns the Skill registry and the long-term
Memory store; OpenHands is the **execution plane** and only ever sees the *selected
subset* for one task. This module renders that subset so that **both** transports
deliver byte-identical text:

* the subprocess transport — ``forgeflow/codeplane/runner/run_code_task.py``
  (its ``_context_block`` delegates here); and
* the agent-server transport —
  ``forgeflow/codeplane/runner/agent_server/conversation.py`` (``_build`` appends
  this block to the agent's system prompt).

Before this module existed the agent-server transport silently dropped the selected
context while the platform's ``codeplane.injected`` audit field still listed it — a
report of an injection that never happened. Keeping exactly one renderer here makes
a cross-transport divergence impossible by construction.

Discipline (runner red line): this module imports **only the standard library** —
no ``openhands`` (it must import in the ForgeFlow venv) and no ``forgeflow`` (it
must import in the OpenHands venv). It is a pure function: the same input mapping
always yields byte-identical output.
"""

from __future__ import annotations

from typing import Any


def render_context_block(source: dict[str, Any]) -> str:
    """Render ForgeFlow's selected context subset for one task (INC27).

    ``source`` is any mapping carrying the two selected lists under
    ``skill_context`` / ``memory_context`` — the runner passes the decoded job,
    the agent server passes the ``agent`` request spec; both therefore render the
    same text for the same selection.

    The block is **appended** to the agent's minimal system prompt, never
    substituted for it. The minimal prompt is load-bearing for the local qwen
    model, so the system prompt must still *start* with it.

    Returns ``""`` when nothing was injected (an honest absence, never a
    placeholder), and renders only the fields the platform really supplied.
    """
    raw_skills = source.get("skill_context")
    raw_memories = source.get("memory_context")
    skills = raw_skills if isinstance(raw_skills, list) else []
    memories = raw_memories if isinstance(raw_memories, list) else []
    if not skills and not memories:
        return ""

    # Render into local lists FIRST: a header is emitted only when something is
    # actually renderable underneath it. Otherwise a context list full of junk
    # would still print "Skills selected for this task" with nothing under it —
    # a hollow claim about what the platform supplied.
    skill_lines: list[str] = []
    for item in skills:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        version = str(item.get("version") or "").strip()
        label = name or str(item.get("id") or "").strip()
        if version:
            label = f"{label} (v{version})" if label else f"v{version}"
        description = str(item.get("description") or "").strip()
        detail = f" - {description}" if description else ""
        steps = item.get("steps")
        step_text = ""
        if isinstance(steps, (list, tuple)) and steps:
            joined = "; ".join(str(s) for s in steps if str(s or "").strip())
            step_text = f" Steps: {joined}." if joined else ""
        rendered = f"- {label}{detail}.{step_text}".rstrip()
        if rendered not in ("-", "- ."):
            skill_lines.append(rendered)

    memory_lines: list[str] = []
    for item in memories:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if content:
            memory_lines.append(f"- {content}")

    # Nothing renderable ⇒ no block at all (an honest absence).
    if not skill_lines and not memory_lines:
        return ""

    lines: list[str] = ["", "Platform context for this task (supplied by ForgeFlow):"]
    if skill_lines:
        lines.append("Skills selected for this task - follow their steps where applicable:")
        lines.extend(skill_lines)
    if memory_lines:
        lines.append("Relevant long-term memory for this repository/task:")
        lines.extend(memory_lines)
    lines.append("Use this context where relevant; it never overrides the rules above.")
    return "\n".join(lines)
