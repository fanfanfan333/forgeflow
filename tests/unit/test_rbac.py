"""Tests for RBAC policy enforcement."""

from __future__ import annotations

from forgeflow.rbac.enforcer import RBACEnforcer


class TestRBACEnforcer:
    def setup_method(self):
        self.enforcer = RBACEnforcer()

    def test_admin_can_do_anything(self):
        assert self.enforcer.check("admin", "execute", "workflows")
        assert self.enforcer.check("admin", "approve", "proposals")
        assert self.enforcer.check("admin", "delete", "anything")

    def test_sales_rep_can_execute_workflows(self):
        assert self.enforcer.check("sales_rep", "execute", "workflows")

    def test_sales_rep_cannot_approve_proposals(self):
        assert not self.enforcer.check("sales_rep", "approve", "proposals")

    def test_manager_can_approve(self):
        assert self.enforcer.check("manager", "approve", "proposals")

    def test_manager_can_execute_workflows(self):
        # manager holds execute:workflows (INC6): it already holds the whole
        # approve:*/write:* surface, so withholding only execute would rank it
        # below sales_rep. Negative controls live in test_viewer_cannot_execute /
        # test_anonymous_denied / test_unknown_role_denied — not weakened here.
        assert self.enforcer.check("manager", "execute", "workflows")

    def test_manager_route_level_execute_is_pinned_against_drift(self):
        """Anti-drift: the route-level effect of manager holding execute:workflows.

        Removing the grant would make the manager home / run dialog silently 403
        (POST /tasks, POST /workflows/run); this pins it so such a change goes
        red, and pins the negative that viewer stays denied.
        """
        from forgeflow.rbac.policies import ROLE_PERMISSIONS, ROUTE_PERMISSION_MAP

        assert "execute:workflows" in ROLE_PERMISSIONS["manager"]

        for route in (("POST", "/tasks"), ("POST", "/workflows/run")):
            action, resource = ROUTE_PERMISSION_MAP[route]
            assert self.enforcer.check("manager", action, resource), route
            assert not self.enforcer.check("viewer", action, resource), route

    def test_viewer_can_read_metrics(self):
        assert self.enforcer.check("viewer", "read", "metrics")

    def test_viewer_cannot_execute(self):
        assert not self.enforcer.check("viewer", "execute", "workflows")

    def test_unknown_role_denied(self):
        assert not self.enforcer.check("hacker", "execute", "workflows")

    def test_anonymous_denied(self):
        assert not self.enforcer.check("anonymous", "read", "metrics")
