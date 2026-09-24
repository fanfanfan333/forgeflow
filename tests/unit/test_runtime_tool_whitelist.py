"""Tool-whitelist unification — one source of truth for the platform's tools.

``required_permission`` used to grant the coarse ``execute:workflows`` run
permission **only** to ``PLATFORM_TOOLS`` (the three default plan steps). Every
other canonical catalogue tool (``analysis.score`` / ``docs.parse`` /
``report.render``) fell through to the fail-closed ``execute:<namespace>``
branch, so an offline ``sales_rep`` — who holds only ``execute:workflows`` — was
barred from tools the platform itself ships, and the planner silently dropped
them from the plan.

The fix routes the whole platform set (``PLATFORM_TOOLS`` ∪
``PLATFORM_TOOL_CATALOGUE`` == ``PLATFORM_PLAN_TOOLS``) through the single
``_RUN_PERMISSION`` grant. Explicit grants (``TOOL_PERMISSION_MAP``) keep their
priority; unknown tools stay fail-closed. These tests pin all of that, the
role × catalogue matrix, and that the trust baseline / planner read the *same*
set (no second source of truth).
"""

from __future__ import annotations

from typing import Any

import pytest

from forgeflow.runtime.gate import (
    PLATFORM_PLAN_TOOLS,
    PLATFORM_TOOL_CATALOGUE,
    PLATFORM_TOOLS,
    TOOL_PERMISSION_MAP,
    check_tool_permission,
    required_permission,
)

_RUN_PERMISSION = ("execute", "workflows")
_CATALOGUE = sorted(PLATFORM_TOOL_CATALOGUE)


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, run_id: str, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


# --------------------------------------------------------------------------- #
# 1. The set is the single union                                               #
# --------------------------------------------------------------------------- #

def test_platform_set_is_the_single_union():
    assert PLATFORM_PLAN_TOOLS == PLATFORM_TOOLS | PLATFORM_TOOL_CATALOGUE
    # The exact members that used to fall through to a fail-closed denial.
    assert {"analysis.score", "docs.parse", "report.render"} <= PLATFORM_PLAN_TOOLS


# --------------------------------------------------------------------------- #
# 2. Every platform plan tool inherits the run permission                      #
# --------------------------------------------------------------------------- #

def test_every_platform_plan_tool_inherits_the_run_permission():
    for tool in sorted(PLATFORM_PLAN_TOOLS):
        assert required_permission(tool) == _RUN_PERMISSION, tool


def test_explicit_grants_keep_priority_and_are_not_swept_in():
    for tool, perm in TOOL_PERMISSION_MAP.items():
        assert required_permission(tool) == perm, tool
        assert perm != _RUN_PERMISSION
        # Money movement / egress / privilege changes must NOT be inherited.
        assert tool not in PLATFORM_PLAN_TOOLS


# --------------------------------------------------------------------------- #
# 3. Unknown tools stay fail-closed                                           #
# --------------------------------------------------------------------------- #

def test_unknown_tool_stays_fail_closed():
    assert required_permission("evil.exec") == ("execute", "evil")
    assert required_permission("acme.widget") == ("execute", "acme")
    assert required_permission("") == ("execute", "workflows")
    assert check_tool_permission("sales_rep", "evil.exec") is False


# --------------------------------------------------------------------------- #
# 4. Role × catalogue matrix — the whole point of the fix                      #
# --------------------------------------------------------------------------- #

#: expected ``check_tool_permission(role, <catalogue tool>)`` is exactly
#: "does this role hold ``execute:workflows`` (or ``*:*``)".
_ROLE_HAS_RUN_PERMISSION = {
    "admin": True,      # *:*
    "sales_rep": True,  # execute:workflows
    "service": True,    # execute:workflows
    "manager": True,    # execute:workflows
    "viewer": False,
    "anonymous": False,
}


@pytest.mark.parametrize("role,expected", sorted(_ROLE_HAS_RUN_PERMISSION.items()))
def test_role_by_catalogue_matrix(role: str, expected: bool):
    for tool in _CATALOGUE:
        assert check_tool_permission(role, tool) is expected, (role, tool)


# --------------------------------------------------------------------------- #
# 5. Single source of truth — baseline + planner read the same set             #
# --------------------------------------------------------------------------- #

def test_tool_sets_share_one_source_of_truth():
    from forgeflow.runtime.llm_planner import known_plan_tools
    from forgeflow.skills.trust_baseline import allowed_tool_set

    expected = set(PLATFORM_PLAN_TOOLS) | set(TOOL_PERMISSION_MAP)
    assert set(known_plan_tools()) == expected
    assert set(allowed_tool_set()) == expected


# --------------------------------------------------------------------------- #
# 6. Gate-allow ⇒ runtime-usable                                               #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_catalogue_tool_is_runtime_usable_for_a_run_role(monkeypatch):
    """A role with ``execute:workflows`` can actually execute a catalogue tool
    through the deterministic executor — not merely pass the gate."""
    import forgeflow.runtime.orchestrator as orch
    from forgeflow.runtime.orchestrator import RequestContext, TaskCreate

    monkeypatch.setattr(
        orch,
        "_DEFAULT_STEPS",
        [{"tool": "report.render", "step_type": "agent", "note": "生成报告"}],
    )
    bus = _RecordingBus()
    ctx = RequestContext(tenant_id="t-cat", user_id="s-1", role="sales_rep")

    steps, errors = await orch._default_executor(
        TaskCreate(intent="生成月度报告"), ctx, bus, "run-cat-1"
    )

    assert errors == []
    assert [s["tool"] for s in steps] == ["report.render"]


@pytest.mark.asyncio
async def test_catalogue_tool_is_still_denied_for_a_role_without_the_run_grant(monkeypatch):
    """The widening must not open the door for a role that lacks the run grant."""
    import forgeflow.runtime.orchestrator as orch
    from forgeflow.runtime.orchestrator import RequestContext, TaskCreate

    monkeypatch.setattr(
        orch,
        "_DEFAULT_STEPS",
        [{"tool": "analysis.score", "step_type": "agent", "note": "打分"}],
    )
    bus = _RecordingBus()
    ctx = RequestContext(tenant_id="t-cat-2", user_id="v-1", role="viewer")

    steps, errors = await orch._default_executor(
        TaskCreate(intent="给线索打分"), ctx, bus, "run-cat-2"
    )

    assert steps == []
    assert errors and "权限不足" in errors[0]
