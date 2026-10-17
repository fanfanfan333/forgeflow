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
from urllib.parse import urlparse

from forgeflow.api.main import app
from forgeflow.bootstrap.runtime import DEFAULT_PORT
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


# --------------------------------------------------------------------------- #
# 7. Vite dev-proxy default target port == backend DEFAULT_PORT                #
# --------------------------------------------------------------------------- #
# The incident this pins: frontend/vite.config.ts defaulted its dev proxy target
# to http://localhost:8000 while the backend binds ``DEFAULT_PORT`` (8010). On a
# host where :8000 is a *different* project, `npm run dev` silently proxied every
# /api call into the wrong service and login failed with an opaque
# 「登录失败（502）：网关错误」. The port is a single fact written twice — in the
# backend constant and in the vite default — and nothing forced them to agree.
# Read the truth from the backend constant (never hardcode 8010) so what is
# pinned is the *contract*, not today's number: either side drifting must go red.
_VITE_CONFIG = _REPO / "frontend/vite.config.ts"
# A real declaration is a bare string literal, optionally `;`-terminated, alone on
# its line. A concatenation such as `'http://localhost:' + '8010'` must NOT match
# — it has to fail with the explicit "declare it as a literal" message instead.
_DEFAULT_TARGET_DECL_RE = re.compile(
    r"""^\s*const\s+DEFAULT_API_TARGET\s*=\s*(?P<quote>["'])(?P<url>[^"']*)(?P=quote)\s*;?\s*$""",
    re.MULTILINE,
)
# `const apiTarget = <expr> ?? <ident>` — the fallback must be an identifier.
_API_TARGET_DECL_RE = re.compile(
    r"""^\s*const\s+apiTarget\s*=\s*[^\n;]*?\?\?\s*(?P<fallback>[A-Za-z_$][\w$]*)\s*;?\s*$""",
    re.MULTILINE,
)
# Any `target: <value>` line declared inside the shared `proxy` object literal.
_PROXY_TARGET_LINE_RE = re.compile(r"^\s*target\s*:\s*(?P<value>[^\n]+?)\s*,?\s*$", re.MULTILINE)


def _strip_ts_comments(source: str) -> str:
    """Return ``source`` with ``//`` and ``/* */`` comments removed.

    Deliberately string-aware. A naive ``//`` strip would amputate
    ``'http://localhost:8010'`` (the URL itself contains ``//``) *and* would let a
    commented-out ``// DEFAULT_API_TARGET = '...'`` hijack the ``re.search``
    below. Tracking string state fixes both. (Backtick template literals are
    treated as plain strings — this config has none; a documented bounded limit.)
    """
    out: list[str] = []
    i = 0
    n = len(source)
    quote: str | None = None
    while i < n:
        ch = source[i]
        if quote is not None:
            out.append(ch)
            if ch == "\\" and i + 1 < n:  # escaped char inside a string
                out.append(source[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
        elif ch in ("'", '"', "`"):
            quote = ch
            out.append(ch)
            i += 1
        elif ch == "/" and i + 1 < n and source[i + 1] == "/":
            while i < n and source[i] != "\n":  # drop the rest of the line
                i += 1
        elif ch == "/" and i + 1 < n and source[i + 1] == "*":
            i += 2
            while i + 1 < n and not (source[i] == "*" and source[i + 1] == "/"):
                i += 1
            i += 2  # consume the closing */
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _vite_code() -> str:
    """Comment-stripped source of ``frontend/vite.config.ts``."""
    return _strip_ts_comments(_VITE_CONFIG.read_text(encoding="utf-8"))


def _declared_api_target(code: str) -> str:
    """The ``DEFAULT_API_TARGET`` string literal (a literal, never an expression)."""
    match = _DEFAULT_TARGET_DECL_RE.search(code)
    assert match is not None, (
        "frontend/vite.config.ts must declare `const DEFAULT_API_TARGET = '<url>'` "
        "as a bare string literal on its own line (not a concatenation/expression)"
    )
    return match.group("url")


def _api_target_fallback(code: str) -> str:
    """The identifier ``apiTarget`` falls back to (``env ?? <ident>``)."""
    match = _API_TARGET_DECL_RE.search(code)
    assert match is not None, (
        "frontend/vite.config.ts must bind `const apiTarget = <expr> ?? <ident>` "
        "with an identifier fallback (a literal here revives the login-502 bug)"
    )
    return match.group("fallback")


def _proxy_target_values(code: str) -> list[str]:
    """Every ``target: <value>`` value declared in the ``proxy`` object."""
    matches = _PROXY_TARGET_LINE_RE.finditer(code)
    return [m.group("value").strip().rstrip(",").strip() for m in matches]


def test_vite_dev_proxy_default_target_port_matches_backend_default_port():
    """The Vite dev proxy's declared default target is the backend's real port.

    A mismatch silently proxies /api to the wrong service (or to nothing), which
    the SPA can only render as a bare 502 — the login-502 regression. On failure
    align the two hardcoded ports; never delete this test.
    """
    target = _declared_api_target(_vite_code())
    parsed = urlparse(target)
    assert parsed.port is not None, (
        f"DEFAULT_API_TARGET must carry an explicit port — got {target!r}"
    )
    assert parsed.port == DEFAULT_PORT, (
        f"vite dev proxy default target {target!r} (port {parsed.port}) != backend "
        f"DEFAULT_PORT {DEFAULT_PORT}; align frontend/vite.config.ts with "
        "forgeflow/bootstrap/runtime.py"
    )


def test_vite_proxy_fallback_references_the_default_target_constant():
    """``apiTarget``'s fallback must be the ``DEFAULT_API_TARGET`` identifier.

    Pinning the declared constant's *value* is not enough: if the fallback is a
    literal (``env ?? 'http://localhost:8000'``), the constant becomes dead
    lettering and the 502 returns while the value-check stays green.
    """
    code = _vite_code()
    fallback = _api_target_fallback(code)
    assert fallback == "DEFAULT_API_TARGET", (
        f"apiTarget falls back to {fallback!r}; it must reference the "
        "DEFAULT_API_TARGET identifier so the declared constant is the real "
        "default (a literal here re-introduces the login-502 regression)"
    )
    # The referenced constant must itself be a real literal declaration.
    _declared_api_target(code)


def test_vite_proxies_use_the_api_target_constant():
    """Every proxy ``target:`` must be ``apiTarget`` — never a re-hardcoded host.

    Covers the /api + /openapi.json proxies. A literal target bypasses the
    constant entirely and is exactly the silent-regression shape this file exists
    to catch.
    """
    values = _proxy_target_values(_vite_code())
    assert len(values) >= 2, (
        f"expected >=2 proxy `target:` entries (/api + /openapi.json), found {values}"
    )
    wrong = [value for value in values if value != "apiTarget"]
    assert wrong == [], f"proxy `target:` must be the apiTarget constant; found {wrong}"


def test_vite_parser_ignores_commented_out_declaration():
    """A commented-out ``DEFAULT_API_TARGET`` must not hijack the parser.

    Guards the ``re.search`` first-match trap directly on a synthetic source, so
    comment immunity cannot silently regress.
    """
    source = "\n".join(
        [
            "// DEFAULT_API_TARGET = 'http://localhost:9999'",
            "/* DEFAULT_API_TARGET = 'http://localhost:9998' */",
            "const DEFAULT_API_TARGET = 'http://localhost:8010'",
        ]
    )
    assert _declared_api_target(_strip_ts_comments(source)) == "http://localhost:8010"
