"""INC7 — fact-source alignment.

The platform keeps making the same class of mistake: a *promise* (a documented
role table, a front-end roster, a route→permission entry) drifts away from the
*reality* (``ROLE_PERMISSIONS``, the routes the app actually serves, the code).
Each test below pins one promise⇄reality seam so the drift can never happen
quietly again. A failure means one side changed without the other following —
fix the source of truth, never weaken the test.
"""

from __future__ import annotations

import re
from pathlib import Path

from forgeflow.api.main import app
from forgeflow.rbac.enforcer import RBACEnforcer
from forgeflow.rbac.policies import ROLE_PERMISSIONS, ROUTE_PERMISSION_MAP
from forgeflow.runtime.gate import PLATFORM_TOOL_CATALOGUE
from forgeflow.skills.registry import _FEATURED_SEED

_REPO = Path(__file__).resolve().parents[2]

# The one authority on "may this role execute a workflow?". Using the enforcer
# (rather than a literal `"execute:workflows" in ROLE_PERMISSIONS[role]`) is what
# makes the check correct for `admin`, whose grant is the wildcard `*:*`.
_ENFORCER = RBACEnforcer()

# Route gates that are DELIBERATELY admin-only: they appear in
# ROUTE_PERMISSION_MAP on purpose but no non-admin role holds them. Declaring
# them here (with a reason) is the honest record — we must NOT widen a role's
# permissions just to make the test green.
_ADMIN_ONLY_GATES: set[tuple[str, str]] = {
    # Creating workspaces is an operator action; only `admin` may do it.
    ("write", "workspaces"),
}

# ``anonymous`` is the unauthenticated marketplace pseudo-role
# (``read:marketplace``). It has no assignable users, carries no documented role
# row and is not listed in the RBAC UI roster, so it is excluded from the
# "roster/docs == backend roles" checks. Every *assignable* role IS included.
_PUBLIC_PSEUDO_ROLES: set[str] = {"anonymous"}

# A markdown table row whose first cell is a (optionally back-ticked) role name.
_ROLE_ROW_RE = re.compile(r"^\|\s*`?([a-z_]+)`?\s*\|(.+)\|\s*$")


def _all_routes() -> set[tuple[str, str]]:
    """(METHOD, path) for every route the app exposes — schema + non-schema.

    Mirrors ``tests/unit/test_route_permissions.py::_all_routes`` — OpenAPI for
    the documented routes plus ``app.routes`` for the ``include_in_schema=False``
    ones.
    """
    routes: set[tuple[str, str]] = set()
    for path, methods in app.openapi().get("paths", {}).items():
        for method in methods:
            routes.add((method.upper(), path))
    for route in app.routes:
        if hasattr(route, "methods") and hasattr(route, "path"):
            for method in getattr(route, "methods", []) or []:
                routes.add((method, route.path))
    return routes


# --------------------------------------------------------------------------- #
# 1. No phantom route→permission entries                                       #
# --------------------------------------------------------------------------- #
def test_no_phantom_route_permission_entries():
    """Every ``(method, prefix)`` in the map must match at least one real route.

    An entry with no matching route is a *phantom* — a promised capability the
    platform does not have. This is exactly what ``("GET", "/context")`` was
    before ``forgeflow/api/routers/context.py`` existed.
    """
    routes = _all_routes()
    phantoms = sorted(
        (method, prefix)
        for (method, prefix) in ROUTE_PERMISSION_MAP
        if not any(m == method and p.startswith(prefix) for (m, p) in routes)
    )
    assert phantoms == [], f"route-permission entries with no real route: {phantoms}"


# --------------------------------------------------------------------------- #
# 2. No gate that only admin can pass (unless declared admin-only)             #
# --------------------------------------------------------------------------- #
def test_every_mapped_permission_has_a_non_admin_holder():
    """Every gated permission must be reachable by a non-admin role.

    A permission held only by ``admin`` is a dead gate for every real user. The
    deliberate exceptions are listed in ``_ADMIN_ONLY_GATES`` — this must never
    be satisfied by loosening a role's grants.
    """
    offenders: list[tuple[str, str]] = []
    for perm in sorted(set(ROUTE_PERMISSION_MAP.values())):
        if perm in _ADMIN_ONLY_GATES:
            continue
        action, resource = perm
        holders = [
            role
            for role, perms in ROLE_PERMISSIONS.items()
            if role != "admin"
            and ("*:*" in perms or f"{action}:{resource}" in perms)
        ]
        if not holders:
            offenders.append(perm)
    assert offenders == [], (
        "permissions reachable only by admin (grant a role, or declare an "
        f"admin-only gate with a reason): {offenders}"
    )


# --------------------------------------------------------------------------- #
# 3. Documented role tables list every permission a role holds                 #
# --------------------------------------------------------------------------- #
def _role_rows(rel_path: str) -> dict[str, str]:
    """Return ``{role: row_text}`` for rows whose first cell names a real role."""
    text = (_REPO / rel_path).read_text(encoding="utf-8")
    rows: dict[str, str] = {}
    for line in text.splitlines():
        match = _ROLE_ROW_RE.match(line)
        if match is None:
            continue
        role = match.group(1)
        if role in ROLE_PERMISSIONS:
            rows[role] = match.group(2)
    return rows


def test_docs_role_tables_are_complete():
    """Each documented role row must mention the action *and* resource of every
    permission that role actually holds (``*:*`` excepted).

    A row that says "read metrics" while the role also holds ``read:leads`` is a
    promise that understates reality — and a reader who trusted it would be
    wrong about what the role can do.
    """
    documented = set(ROLE_PERMISSIONS) - _PUBLIC_PSEUDO_ROLES

    for rel_path in ("docs/auth.md", "docs/api-reference.md"):
        rows = _role_rows(rel_path)
        missing_rows = sorted(documented - set(rows))
        assert missing_rows == [], f"{rel_path}: missing role rows: {missing_rows}"

        for role in sorted(documented):
            row = rows[role]
            for perm in sorted(ROLE_PERMISSIONS[role]):
                if perm == "*:*":
                    continue
                action, resource = perm.split(":", 1)
                assert action in row and resource in row, (
                    f"{rel_path}: role '{role}' row omits '{perm}' "
                    f"(must contain both '{action}' and '{resource}')"
                )


# --------------------------------------------------------------------------- #
# 4. Front-end RBAC roster == backend roles                                    #
# --------------------------------------------------------------------------- #
_RBAC_NAME_RE = re.compile(r"name:\s*'([a-z_]+)'")


def test_frontend_rbac_roster_matches_backend_roles():
    """``RbacView.tsx`` must list exactly the assignable backend roles.

    Fixes the drift this increment found: the view listed a phantom ``analyst``
    role that does not exist in ``ROLE_PERMISSIONS``.
    """
    src = (_REPO / "frontend/src/views/RbacView.tsx").read_text(encoding="utf-8")
    roster = set(_RBAC_NAME_RE.findall(src))
    expected = set(ROLE_PERMISSIONS) - _PUBLIC_PSEUDO_ROLES
    assert roster == expected, (
        f"RbacView roster {sorted(roster)} != backend assignable roles "
        f"{sorted(expected)}"
    )


# --------------------------------------------------------------------------- #
# 5. Home role config's execute capability == backend capability               #
# --------------------------------------------------------------------------- #
def _home_role_keys(src: str) -> set[str]:
    """Roles declared in ``export type Role = 'a' | 'b' | ...``."""
    match = re.search(r"export type Role\s*=\s*([^\n]+)", src)
    assert match is not None, "roleConfig.ts must declare `export type Role = ...`"
    return set(re.findall(r"'([a-z_]+)'", match.group(1)))


def _home_can_execute(src: str) -> dict[str, bool]:
    """``{role: canExecute}`` parsed from each 2-space-indented role block."""
    flags: dict[str, bool] = {}
    current: str | None = None
    for line in src.splitlines():
        role = re.match(r"^  ([a-z_]+):\s*\{\s*$", line)
        if role is not None:
            current = role.group(1)
            continue
        flag = re.search(r"canExecute:\s*(true|false)", line)
        if flag is not None and current is not None:
            flags[current] = flag.group(1) == "true"
    return flags


def test_frontend_home_roles_match_backend_execute_capability():
    """A front-end role must advertise execution iff the backend grants it.

    This is the mechanism that catches the viewer drift: the home hero offered
    task chips to ``viewer`` even though ``viewer`` lacks ``execute:workflows``,
    so submitting them always 403'd.
    """
    src = (_REPO / "frontend/src/home/roleConfig.ts").read_text(encoding="utf-8")
    keys = _home_role_keys(src)
    unknown = keys - set(ROLE_PERMISSIONS)
    assert unknown == set(), f"front-end roles absent from the backend: {sorted(unknown)}"

    caps = _home_can_execute(src)
    for role in sorted(keys):
        # Enforcer semantics: `admin`'s `*:*` grants execute just like an
        # explicit `execute:workflows`, so the wildcard is handled here.
        expected = _ENFORCER.check(role, "execute", "workflows")
        assert caps.get(role) is expected, (
            f"roleConfig '{role}'.canExecute={caps.get(role)} but backend "
            f"execution capability is {expected}"
        )


# --------------------------------------------------------------------------- #
# 6. Seed skills only reference platform-catalogue tools                       #
# --------------------------------------------------------------------------- #
def test_seed_skill_tools_are_in_the_platform_catalogue():
    """Every tool a seeded skill declares must be a shipped platform tool.

    A tool outside ``PLATFORM_TOOL_CATALOGUE`` resolves fail-closed to
    ``execute:<namespace>`` at runtime, so a seed naming one would be a skill
    that cannot actually run for any non-admin role.
    """
    declared = {tool for seed in _FEATURED_SEED for tool in seed.get("tools", [])}
    undeclared = sorted(declared - PLATFORM_TOOL_CATALOGUE)
    assert undeclared == [], (
        f"seed skills reference tools absent from PLATFORM_TOOL_CATALOGUE: {undeclared}"
    )
