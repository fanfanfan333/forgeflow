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
    "CODE_TOOL_PERMISSIONS",
    "PLATFORM_PLAN_TOOLS",
    "PLATFORM_TOOLS",
    "TOOL_PERMISSION_MAP",
    "check_code_permission",
    "check_tool_permission",
    "describe_code_denial",
    "describe_denial",
    "required_code_permission",
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
        # INC26 Q5 — the real deterministic data-analysis step (stdlib csv). It
        # sits in the catalogue (hence in ``PLATFORM_PLAN_TOOLS``) so it inherits
        # the coarse ``execute:workflows`` run grant — exactly like
        # ``analysis.score`` — and is deliberately **not** in
        # ``TOOL_PERMISSION_MAP`` (it needs no narrow grant).
        "analysis.profile",
        "docs.parse",
        "report.render",
        # Shipped catalogue tools implemented in mcp/server/tools/platform_tools.py.
        # The contract-review (合同审查) and code-quality (代码质量检查) seed skills
        # declare these, so they belong here rather than resolving to the
        # fail-closed ``execute:<namespace>`` gap.
        "policy.check",
        "git.diff",
        "code.lint",
        # INC25 W2 — the code-execution plane steps. Both live in the catalogue
        # (and therefore inherit the coarse ``execute:workflows`` run grant) so a
        # role that may start a run can drive a code task end to end. The
        # human-in-the-loop *gate* on an actual commit is **not** RBAC — it is the
        # ``code.commit`` handler returning ``awaiting_approval`` until an
        # ApprovalRecord for the run is granted (design §11 U2 / U5). Keeping them
        # out of ``TOOL_PERMISSION_MAP`` is deliberate: the pair must stay in
        # ``PLATFORM_PLAN_TOOLS`` (design §10 drift rule ③ and the orphan guard in
        # tests/unit/test_inc12_orphan_tools.py), and
        # tests/unit/test_runtime_tool_whitelist.py::test_explicit_grants_keep_priority_and_are_not_swept_in
        # forbids any ``TOOL_PERMISSION_MAP`` key from being swept into that set —
        # the two rules are only jointly satisfiable this way.
        "code.execute",
        "code.commit",
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


# --------------------------------------------------------------------------- #
# INC27 — the CODE-PLANE gate (the architecture note's Policy Engine).          #
#                                                                              #
# The note is explicit that the model must not be obeyed blindly:              #
#   "Qwen3:8B 提出 Action → ForgeFlow Policy 允许 / 拒绝 / 修改 → OpenHands 执行"  #
#                                                                              #
# ``required_permission`` is deliberately NOT touched: ``code.execute`` stays    #
# in ``PLATFORM_PLAN_TOOLS`` (the orphan guard and the middleware/gate boundary  #
# test both pin that mapping to ``execute:workflows``). This map is an ADDITIVE  #
# second gate — a code-plane step must clear both the coarse run grant and this  #
# narrow one, so "may start a run" no longer implies "may drive the code agent". #
# --------------------------------------------------------------------------- #

#: Code-plane tool → the narrow permission it additionally requires.
CODE_TOOL_PERMISSIONS: dict[str, str] = {
    "code.execute": "run:code",
}


def required_code_permission(tool: str) -> str | None:
    """The extra code-plane permission ``tool`` needs (``None`` when none)."""
    return CODE_TOOL_PERMISSIONS.get((tool or "").strip())


def check_code_permission(role: str, tool: str) -> bool:
    """Return ``True`` when ``role`` may drive this code-plane step.

    Default-allow only in the sense that a non-code tool has no extra
    requirement; a code-plane tool without a grant is **denied** (fail-closed).
    """
    needed = required_code_permission(tool)
    if needed is None:
        return True
    permissions = ROLE_PERMISSIONS.get(role, set())
    if "*:*" in permissions:
        return True
    return needed in permissions


def describe_code_denial(role: str, tool: str) -> str:
    """Human-readable code-plane denial (surfaces in ``run.error`` / the UI)."""
    needed = required_code_permission(tool) or "run:code"
    return (
        f"权限不足：角色 '{role}' 缺少 {needed}；"
        f"代码执行步骤 '{tool}' 已被代码面策略拦截（执行前中止，未创建任何运行）"
    )
