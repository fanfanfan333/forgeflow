"""INC2-21 — per-step RBAC re-check inside the Agent Runtime.

The route-level gate (``RBACMiddleware`` + ``ROUTE_PERMISSION_MAP``) only ever
sees the coarse pair ``execute:workflows`` for ``POST /tasks``. That answers
"may this role start a run?" — it says nothing about *which tools* the run may
then invoke. Previously ``orchestrator._default_executor`` went straight from
"run accepted" to the risk gate, so a role that may start a run could execute
any tool the plan named.

This module closes that gap: every step is re-checked against RBAC **before**
the risk gate and **before** the tool runs. A denial produces ``run.error`` and
returns immediately, so no tool side effect can occur.

Permission model:

* ``TOOL_PERMISSION_MAP`` — tools that need an explicit, narrow grant
  (money movement, data egress, privilege changes).
* ``PLATFORM_PLAN_TOOLS`` — every tool the platform itself ships: the default
  plan steps (``PLATFORM_TOOLS``) plus the wider skill catalogue
  (``PLATFORM_TOOL_CATALOGUE``). Any identity already allowed to execute a
  workflow (``execute:workflows``) may run these; that is what makes an offline
  ``sales_rep`` run able to execute the shipped catalogue at all.
* Anything else (an unknown / third-party tool) resolves fail-closed to
  ``execute:<namespace>``, so a role without that grant is denied.
"""

from __future__ import annotations

from forgeflow.rbac.enforcer import RBACEnforcer
from forgeflow.rbac.policies import ROLE_PERMISSIONS

__all__ = [
    "PLATFORM_PLAN_TOOLS",
    "PLATFORM_TOOLS",
    "TOOL_PERMISSION_MAP",
    "check_tool_permission",
    "describe_denial",
    "required_permission",
]

# Tools that require a specific, narrow grant — never inherited from the
# coarse "execute:workflows" route permission.
TOOL_PERMISSION_MAP: dict[str, tuple[str, str]] = {
    "payment.transfer": ("approve", "proposals"),
    "payment.refund": ("approve", "proposals"),
    "data.export": ("write", "memory"),
    "policy.grant": ("write", "policies"),
    "skill.publish": ("approve", "skills"),
}

# The deterministic default plan (Supervisor → Research → Data → Code).
PLATFORM_TOOLS: frozenset[str] = frozenset(
    {"research.search", "data.query", "code.run"}
)

# The canonical catalogue of tools the platform itself ships — the built-in
# agent steps referenced by the seed skills (skills/registry.py) and the
# compiler's fallback plan (candidate_compiler._FALLBACK_TOOLS). A skill spec
# may declare any of these; anything outside requires an explicit grant.
PLATFORM_TOOL_CATALOGUE: frozenset[str] = frozenset(
    {
        "research.search",
        "data.query",
        "analysis.score",
        "docs.parse",
        "report.render",
        # Shipped catalogue tools implemented in mcp/server/tools/platform_tools.py.
        # The contract-review (合同审查) and code-quality (代码质量检查) seed skills
        # declare these, so they belong here rather than resolving to the
        # fail-closed ``execute:<namespace>`` gap.
        "policy.check",
        "git.diff",
        "code.lint",
    }
)

# The **single source of truth** for "a platform-built-in plan step".
#
# Every tool the platform itself ships — the deterministic default steps
# (``PLATFORM_TOOLS``) *and* the wider skill catalogue
# (``PLATFORM_TOOL_CATALOGUE``) — inherits the coarse ``_RUN_PERMISSION``: that
# is what makes an offline ``sales_rep`` run (which only ever holds
# ``execute:workflows``) able to execute the whole shipped catalogue, not just
# the three default steps. ``TOOL_PERMISSION_MAP`` (money movement, egress,
# privilege changes) stays separately gated and is deliberately *not* in here.
#
# ``skills.trust_baseline.allowed_tool_set`` and
# ``runtime.llm_planner.known_plan_tools`` import this constant so the runtime
# RBAC check, the pre-publication baseline and the planner can never drift.
PLATFORM_PLAN_TOOLS: frozenset[str] = PLATFORM_TOOLS | PLATFORM_TOOL_CATALOGUE

# Permission every run already had to hold to reach the runtime at all.
_RUN_PERMISSION: tuple[str, str] = ("execute", "workflows")

_ENFORCER = RBACEnforcer()


def required_permission(tool: str) -> tuple[str, str]:
    """Resolve a tool name to the ``(action, resource)`` pair it needs.

    Unknown tools resolve to ``execute:<namespace>`` (fail-closed for every
    role that does not hold that explicit grant), never to a wildcard.
    """
    name = (tool or "").strip()
    if name in TOOL_PERMISSION_MAP:
        return TOOL_PERMISSION_MAP[name]
    if name in PLATFORM_PLAN_TOOLS:
        return _RUN_PERMISSION
    namespace = name.split(".")[0] if "." in name else ""
    return ("execute", namespace or "workflows")


def check_tool_permission(role: str, tool: str) -> bool:
    """Return ``True`` when ``role`` may invoke ``tool`` inside a run."""
    action, resource = required_permission(tool)
    permissions = ROLE_PERMISSIONS.get(role, set())
    if "*:*" in permissions:
        return True
    return _ENFORCER.check(role, action, resource)


def describe_denial(role: str, tool: str) -> str:
    """Human-readable denial reason — surfaces in ``run.error`` and the UI."""
    action, resource = required_permission(tool)
    return (
        f"权限不足：角色 '{role}' 缺少 {action}:{resource}；"
        f"工具 '{tool}' 已被运行时 RBAC 拦截（执行前中止）"
    )
