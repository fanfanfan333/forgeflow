"""INC10 F4 — the runtime gate owns *tool-level* RBAC; the route middleware does not.

``目标.md`` §7 line 8 left open whether the in-run gate duplicates the route
middleware. The binary split the code actually implements is:

* ``middleware/auth.RBACMiddleware`` + ``ROUTE_PERMISSION_MAP`` resolve a
  *(method, path prefix)* to a **coarse** ``(action, resource)`` pair by
  longest-prefix match — ``POST /tasks`` → ``execute:workflows``. It never
  parses a tool name (it cannot: ``/tasks`` carries no tool).
* ``runtime/gate.required_permission`` resolves a **tool name** to its
  ``(action, resource)``: ``PLATFORM_PLAN_TOOLS`` → ``execute:workflows``; an
  unknown tool → ``execute:<namespace>`` (fail-closed, never a wildcard); and
  ``TOOL_PERMISSION_MAP`` for the narrow money / egress / privilege grants.

What these nails actually guarantee (and what they do NOT)
----------------------------------------------------------
Every guard on the middleware source is **static** and therefore *finite*. They
catch the two *structural* regressions the split can suffer; they are **not** a
proof. Read the residual limit at the bottom before trusting them.

* :func:`test_route_middleware_does_not_import_the_tool_gate` — an **AST** scan
  that the middleware never imports the tool gate: absolute
  (``import forgeflow.runtime.gate``), package
  (``import forgeflow.runtime``), ``from``-form
  (``from forgeflow.runtime import gate``) and relative form
  (``from ..runtime import gate``). A *dynamic* import
  (``importlib.import_module("forgeflow.runtime.gate")``) is invisible to an
  import-statement scan and is covered only by the next nail.
* :func:`test_route_middleware_has_no_tool_to_permission_mapping` — an **AST**
  scan that the middleware holds no "dotted tool name → (action, resource)
  string pair" mapping (the ``_MW_TOOL_GRANTS = {"payment.transfer":
  ("approve", "proposals")}`` shape), and no computed attribute name
  (``getattr(obj, "required_" + "permission")``) used to evade the name check.
* :func:`test_route_middleware_does_not_resolve_tool_permissions` — the older,
  weaker **string** check for the five canonical symbol names. Kept because it
  cheaply catches plain symbol reuse.

**Residual limit — architectural, and NOT fixable by adding more static
assertions.** An inlined copy that renames the symbols, the dict keys *and* the
shape, or a fully dynamic call such as
``importlib.import_module("forgeflow.runtime.gate").required_permission``, still
passes all of the above. The invariant "tool-level resolution lives only in
``gate.py``" is a property of the architecture; these nails only keep the
*obvious* copies out. What actually pins the mechanism are the behavioural
assertions on ``gate.required_permission`` below.
"""

from __future__ import annotations

import ast
import pathlib

from forgeflow.middleware.auth import RBACMiddleware
from forgeflow.rbac.policies import ROUTE_PERMISSION_MAP
from forgeflow.runtime import gate

_REPO = pathlib.Path(__file__).resolve().parents[2]
_MIDDLEWARE = _REPO / "forgeflow" / "middleware" / "auth.py"
_GATE = _REPO / "forgeflow" / "runtime" / "gate.py"

#: Tool-level permission symbols. Their appearance in the *middleware* source
#: would mean the coarse route gate had started resolving tools.
_TOOL_LEVEL_SYMBOLS = (
    "TOOL_PERMISSION_MAP",
    "PLATFORM_PLAN_TOOLS",
    "PLATFORM_TOOL_CATALOGUE",
    "required_permission",
    "check_tool_permission",
)

#: The module that owns tool-level RBAC — the middleware must never reach it.
_TOOL_GATE_MODULE = "forgeflow.runtime.gate"
#: Package that *exposes* the gate module (``forgeflow.runtime.gate``).
_TOOL_GATE_PACKAGE = "forgeflow.runtime"
#: This file's package, used to resolve relative imports (``from ..runtime``).
_MIDDLEWARE_PACKAGE = "forgeflow.middleware"


# --------------------------------------------------------------------------- #
# AST helpers                                                                   #
# --------------------------------------------------------------------------- #

def _middleware_tree() -> ast.Module:
    return ast.parse(_MIDDLEWARE.read_text(encoding="utf-8"), filename=str(_MIDDLEWARE))


def _absolute_module(node: ast.ImportFrom) -> str:
    """Resolve an ``ImportFrom`` target module for absolute *and* relative forms.

    ``auth.py`` lives in package ``forgeflow.middleware``; ``level=1`` is that
    package, ``level=2`` its parent ``forgeflow``, and so on.
    """
    if not node.level:
        return node.module or ""
    parts = _MIDDLEWARE_PACKAGE.split(".")
    keep = len(parts) - (node.level - 1)
    base = parts[:keep] if keep > 0 else []
    if not base:
        return node.module or ""
    return ".".join([*base, node.module]) if node.module else ".".join(base)


def _gate_imports(tree: ast.Module) -> list[str]:
    """Return every import in ``tree`` that brings the tool gate into scope."""
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if (
                    name == _TOOL_GATE_MODULE
                    or name.startswith(_TOOL_GATE_MODULE + ".")
                    or name == _TOOL_GATE_PACKAGE
                ):
                    offenders.append(f"import {name}")
        elif isinstance(node, ast.ImportFrom):
            mod = _absolute_module(node)
            names = [a.name for a in node.names]
            if mod == _TOOL_GATE_MODULE:
                offenders.append(f"from {mod} import {', '.join(names) or '(...)'}")
            elif mod == _TOOL_GATE_PACKAGE and "gate" in names:
                offenders.append(f"from {mod} import {', '.join(names)}")
            elif mod == "forgeflow" and "runtime" in names:
                # `from forgeflow import runtime` makes `runtime.gate` reachable.
                offenders.append(f"from {mod} import {', '.join(names)}")
    return offenders


def _is_dotted_str(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and "." in node.value


def _is_permission_pair(node: ast.AST) -> bool:
    """A ``(action, resource)`` shape: a 2-element tuple/list/set of str consts."""
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return len(node.elts) == 2 and all(
            isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts
        )
    return False


def _tool_mapping_offences(tree: ast.Module) -> list[str]:
    """Return every tool→permission mapping or computed-attr evasion in ``tree``."""
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if key is not None and _is_dotted_str(key) and _is_permission_pair(value):
                    offenders.append(f"tool→permission dict entry keyed {key.value!r}")
                    break
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], (ast.BinOp, ast.JoinedStr))
        ):
            offenders.append("computed getattr(...) attribute name (name-check evasion)")
    return offenders


# --------------------------------------------------------------------------- #
# 1. the middleware stays a prefix-only *route* gate                            #
# --------------------------------------------------------------------------- #

def test_route_middleware_does_not_resolve_tool_permissions():
    """The middleware stays a prefix-only *route* gate (no tool parsing).

    Static *string* check for the canonical symbol names — the cheapest guard,
    and the one an honest reuse of ``gate``'s symbols trips first.
    """
    source = _MIDDLEWARE.read_text(encoding="utf-8")
    for symbol in _TOOL_LEVEL_SYMBOLS:
        assert symbol not in source, (
            "middleware/auth.py must stay a *route-level* gate — it must not "
            f"reference the tool-level symbol {symbol!r} (INC10 F4)"
        )


def test_route_middleware_does_not_import_the_tool_gate():
    """AST dependency-graph guard (INC10 F4-QA).

    The middleware must not import the tool gate in any form. This is an AST
    scan, not a substring match, so comments/strings cannot satisfy it.
    """
    offenders = _gate_imports(_middleware_tree())
    assert offenders == [], (
        "middleware/auth.py must not import the tool gate "
        f"({_TOOL_GATE_MODULE}); found: {offenders}"
    )


def test_route_middleware_has_no_tool_to_permission_mapping():
    """AST structural guard (INC10 F4-QA).

    No "dotted tool name → (action, resource)" mapping may exist in the
    middleware, and no computed ``getattr`` name may be used to evade the
    symbol check. Mirrors the bypass that defeated the plain string nail.
    """
    offenders = _tool_mapping_offences(_middleware_tree())
    assert offenders == [], (
        "middleware/auth.py must not resolve a tool name to a permission — "
        f"found: {offenders}"
    )


# --------------------------------------------------------------------------- #
# 2. the runtime gate owns tool-level resolution (behaviour)                    #
# --------------------------------------------------------------------------- #

def test_runtime_gate_owns_tool_level_resolution():
    """Tool-level RBAC lives in ``gate.py`` — both its source and behaviour."""
    source = _GATE.read_text(encoding="utf-8")
    for symbol in ("TOOL_PERMISSION_MAP", "PLATFORM_PLAN_TOOLS", "required_permission"):
        assert symbol in source, (
            "runtime/gate.py must own tool-level RBAC — missing "
            f"{symbol!r} (INC10 F4); do not move tool resolution out of gate.py"
        )
    # Behaviour the route middleware provably cannot express (it sees no tool).
    assert gate.required_permission("research.search") == ("execute", "workflows")
    assert gate.required_permission("payment.transfer") == ("approve", "proposals")
    assert gate.required_permission("evil.exec") == ("execute", "evil")  # fail-closed


def test_route_gate_sees_only_the_coarse_run_permission():
    """``POST /tasks`` maps to the coarse pair, not to any specific tool."""
    assert ROUTE_PERMISSION_MAP[("POST", "/tasks")] == ("execute", "workflows")
    assert RBACMiddleware._resolve_permission("POST", "/tasks") == ("execute", "workflows")
    # The route gate can't tell the tools apart; only the runtime gate can.
    assert gate.required_permission("payment.transfer") != gate.required_permission(
        "research.search"
    )
