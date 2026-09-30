"""INC27 — the code-plane Policy Engine gate (architecture note §2).

The note is explicit that the model must not be obeyed blindly::

    Qwen3:8B 提出 Action → ForgeFlow Policy 允许 / 拒绝 / 修改 → OpenHands 执行

So "may start a run" (``execute:workflows``) must **not** imply "may drive the
code agent". These tests pin that separation, and pin the *default-deny* side:
a role without ``run:code`` is refused even though it can still run ordinary
(non-code) tasks — which is what makes the gate code-specific rather than a
blanket execution ban.
"""

from __future__ import annotations

import pytest

from forgeflow.rbac.policies import ROLE_PERMISSIONS
from forgeflow.runtime.gate import (
    CODE_TOOL_PERMISSIONS,
    check_code_permission,
    check_tool_permission,
    describe_code_denial,
    required_code_permission,
)

#: Roles that must be able to drive the code plane (see the manager rationale in
#: ``rbac/policies.py`` — manager already held ``approve:skills`` and could
#: approve a code change before INC27, so narrowing it would be a regression).
_PRIVILEGED = ("admin", "manager")

#: Roles that must NOT. ``sales_rep`` is the important one: it holds
#: ``execute:workflows``, so proving it is refused for ``code.execute`` is what
#: distinguishes a code-specific gate from a blanket execution ban.
_UNPRIVILEGED = ("sales_rep", "viewer", "service")


def test_code_execute_requires_its_own_grant():
    assert required_code_permission("code.execute") == "run:code"


def test_non_code_tools_carry_no_extra_requirement():
    """A non-code step is untouched — the gate is additive, never a blanket ban."""
    assert required_code_permission("research.search") is None
    assert required_code_permission("analysis.profile") is None
    assert required_code_permission("report.render") is None
    assert required_code_permission("") is None


@pytest.mark.parametrize("role", _PRIVILEGED)
def test_privileged_roles_may_drive_the_code_agent(role):
    assert check_code_permission(role, "code.execute") is True


@pytest.mark.parametrize("role", _UNPRIVILEGED)
def test_unprivileged_roles_are_denied_default_deny(role):
    assert check_code_permission(role, "code.execute") is False, (
        f"'{role}' must not drive the code agent without 'run:code'"
    )


def test_denied_roles_keep_their_non_code_abilities():
    """The gate is code-specific, not a blanket execution ban.

    Every unprivileged role is untouched for a non-code step (the code-plane map
    simply does not apply). The *proof* of code-specificity is ``sales_rep``: it
    holds the coarse ``execute:workflows`` grant — so it still runs ordinary
    steps — yet it is refused ``code.execute``. (``viewer`` cannot run ordinary
    steps either, since it holds no run grant at all.)
    """
    for role in _UNPRIVILEGED:
        assert check_code_permission(role, "research.search") is True, role

    assert "execute:workflows" in ROLE_PERMISSIONS["sales_rep"]
    assert check_tool_permission("sales_rep", "research.search") is True
    assert check_code_permission("sales_rep", "code.execute") is False


def test_unknown_role_is_denied():
    """Fail-closed: a role absent from the matrix gets nothing."""
    assert check_code_permission("not-a-real-role", "code.execute") is False
    assert check_code_permission("", "code.execute") is False


def test_denial_names_the_missing_permission():
    message = describe_code_denial("sales_rep", "code.execute")
    assert "run:code" in message
    assert "sales_rep" in message
    assert "code.execute" in message


def test_privileged_grants_are_declared():
    """The permission strings the gate reads must exist in the role matrix."""
    for role in _PRIVILEGED:
        permissions = ROLE_PERMISSIONS[role]
        assert "*:*" in permissions or "run:code" in permissions, role
        assert "*:*" in permissions or "approve:code" in permissions, role
    for role in _UNPRIVILEGED:
        assert "run:code" not in ROLE_PERMISSIONS[role], role
        assert "approve:code" not in ROLE_PERMISSIONS[role], role


def test_code_map_stays_out_of_the_general_tool_map():
    """The invariant ``runtime/gate`` documents: the code-plane map is additive.

    ``code.execute`` must remain inside ``PLATFORM_PLAN_TOOLS`` (the orphan guard
    pins it there); the extra grant lives in ``CODE_TOOL_PERMISSIONS`` so the
    general ``required_permission`` mapping is never mutated.
    """
    from forgeflow.runtime.gate import PLATFORM_PLAN_TOOLS, TOOL_PERMISSION_MAP

    assert "code.execute" in PLATFORM_PLAN_TOOLS
    assert "code.execute" not in TOOL_PERMISSION_MAP
    assert set(CODE_TOOL_PERMISSIONS) == {"code.execute"}
