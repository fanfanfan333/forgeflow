"""RBAC policy definitions — role → allowed permissions, route → required permission.

Hardening (SECURITY_AUDIT.md API5):
  - Every route exposed by the API is mapped here. Unmapped requests are
    denied by RBACMiddleware (fail closed).
  - Longest-prefix match in the middleware so /workflows/{id}/trace doesn't
    inherit /workflows's broad "read:workflows" grant.
  - 'service' role is for service-to-service JWTs (sub starts with "service:");
    it intentionally does NOT carry *:* — pick the precise permissions.
"""

from __future__ import annotations

# Role → set of "action:resource" permission strings. "*:*" = unrestricted.
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin": {"*:*"},
    "manager": {
        "read:workflows",
        "read:metrics",
        "read:leads",
        "read:proposals",
        "approve:proposals",
        "read:agents",
        "send:agents",
        "read:memory",
        "read:audit",
        "read:workspaces",
        "read:marketplace",
        "write:marketplace",
        "manage:self",
        # Skill / Policy hubs (docs §4.1)
        "read:skills",
        "write:skills",
        "approve:skills",
        "read:policies",
        "write:policies",
        # execute:workflows — granted deliberately (INC6 RBAC-surface ruling).
        # Do NOT "harden" this back away. Rationale:
        #   * manager already holds the whole approve:*/write:* surface; denying
        #     only execute:workflows would rank manager BELOW sales_rep — a
        #     privilege inversion.
        #   * the product promises it: RunSalesOpsDialog tells users to "sign in
        #     as rep-1 or manager-1", and roleConfig recommends execution tasks on
        #     the manager home (useCreateTask → POST /tasks, else 403).
        #   * demo_users seeds manager-1 as manager and AuthControls defaults the
        #     dev login to manager-1, so the default demo login must be able to
        #     trigger a run.
        #   * platform_tools.policy_check self-documented this as "deliberately
        #     not decided here"; it is now decided: manager CAN execute.
        "execute:workflows",
    },
    "sales_rep": {
        "execute:workflows",
        "read:workflows",
        "read:metrics",
        "read:memory",
        "write:memory",
        "read:agents",
        "read:marketplace",
        "manage:self",
        "read:skills",
    },
    "viewer": {
        "read:metrics",
        "read:workflows",
        "read:marketplace",
        "manage:self",
        # Read-only hub access so the console/home page renders for viewers.
        "read:skills",
        "read:policies",
    },
    "anonymous": {
        # Marketplace listing is intentionally public — discovery is the point.
        "read:marketplace",
    },
    # Narrow service identity — pick exactly what the cron / dispatcher needs.
    "service": {
        "read:metrics",
        "read:workflows",
        "execute:workflows",
        "read:skills",
    },
}

# Maps HTTP (method, path prefix) → (action, resource).
# Longest-prefix match wins. Every route MUST appear here — RBACMiddleware
# denies unmapped routes (fail-closed). When you add a new router, add its
# entries here in the same commit.
ROUTE_PERMISSION_MAP: dict[tuple[str, str], tuple[str, str]] = {
    # --- auth: self-service MFA (authenticated; every real role manages own) ---
    ("POST", "/auth/mfa"): ("manage", "self"),
    # --- workflows ---
    ("POST",   "/workflows/run"):                 ("execute", "workflows"),
    ("POST",   "/workflows/stream"):              ("execute", "workflows"),
    ("GET",    "/workflows"):                     ("read",    "workflows"),
    # Trace (/workflows/{id}/trace) exposes prompts + LLM output. It matches the
    # "/workflows" prefix above (read:workflows); object-level ownership is
    # enforced in the handler (see routers/workflows.py).
    # --- approvals ---
    ("POST",   "/approvals"):                     ("approve", "proposals"),
    ("GET",    "/approvals"):                     ("read",    "proposals"),
    # --- agents ---
    ("GET",    "/agents"):                        ("read",    "agents"),
    ("POST",   "/agents"):                        ("send",    "agents"),
    # --- memory ---
    ("GET",    "/memory"):                        ("read",    "memory"),
    ("POST",   "/memory"):                        ("write",   "memory"),
    ("DELETE", "/memory"):                        ("write",   "memory"),
    # --- metrics ---
    ("GET",    "/metrics"):                       ("read",    "metrics"),
    # --- cost optimization (INC2 A1). Distinct prefix so it never collides
    #     with /metrics; the SPA reads the budget board + savings view. ---
    ("GET",    "/cost"):                          ("read",    "metrics"),
    # --- audit ---
    ("GET",    "/audit"):                         ("read",    "audit"),
    # --- context (Experience/Memory Hub context builder view) ---
    ("GET",    "/context"):                       ("read",    "memory"),
    # --- workspaces ---
    ("GET",    "/workspaces"):                    ("read",    "workspaces"),
    ("POST",   "/workspaces"):                    ("write",   "workspaces"),
    # --- marketplace ---
    ("GET",    "/marketplace"):                   ("read",    "marketplace"),
    # publish / install / rate are POST-only and were previously unmapped ⇒ the
    # middleware returned 403 for every one of them (real defect). The longer
    # "/marketplace/templates/refresh" prefix below still wins for that path.
    ("POST",   "/marketplace"):                   ("write",   "marketplace"),
    ("POST",   "/marketplace/templates/refresh"): ("write",   "marketplace"),
    # --- tasks / runs (AgentFlow runtime; docs §4.1) ---
    ("POST",   "/tasks"):                         ("execute", "workflows"),
    ("GET",    "/runs"):                          ("read",    "workflows"),
    ("POST",   "/runs"):                          ("execute", "workflows"),
    # --- experiences (Memory Hub) ---
    ("GET",    "/experiences"):                   ("read",    "memory"),
    ("POST",   "/experiences"):                   ("write",   "memory"),
    # --- skills (Skill Hub). Longest-prefix match means /skills/{id}/versions
    #     and /skills/{id}/rollback inherit these entries. ---
    ("GET",    "/skills"):                        ("read",    "skills"),
    ("POST",   "/skills"):                        ("write",   "skills"),
    # --- skill candidates. POST /skill-candidates/{id}/promote additionally
    #     requires approve:skills — enforced in the handler (routers/skills.py)
    #     because the path prefix can't express it. ---
    ("GET",    "/skill-candidates"):              ("read",    "skills"),
    ("POST",   "/skill-candidates"):              ("write",   "skills"),
    # --- policies ---
    ("GET",    "/policies"):                      ("read",    "policies"),
    ("POST",   "/policies"):                      ("write",   "policies"),
    # /policies/evaluate is read-only decisioning — read:policies (longer prefix wins).
    ("POST",   "/policies/evaluate"):             ("read",    "policies"),
    # --- security overview (home-page aggregation) ---
    ("GET",    "/security"):                      ("read",    "audit"),
}
