"""INC46 T04 — tool privilege classification (``READ/WRITE/EXTERNAL/DANGEROUS``).

The skill sandbox needs a *four-level* privilege model so a candidate skill can
be reasoned about (and, in ``tester``'s restricted mode, simulated) without ever
touching production side effects. The classification is **derived** — never a
second, hand-maintained tool table:

* the single source of truth is the runtime's tool table
  (``runtime.gate.PLATFORM_PLAN_TOOLS`` / ``TOOL_PERMISSION_MAP``);
* :func:`classify_tool` applies a *fixed, ordered rule set* (below) to a tool
  id — it does not carry a parallel ``{tool: class}`` dictionary that could
  drift from ``gate``;
* ``code.commit`` is folded into ``DANGEROUS`` by rule ① because it is the one
  catalogue tool that performs an irreversible, human-gated write (落库提交).

Rule order (first match wins — mirrors the design §1.4):

1. ``tool ∈ TOOL_PERMISSION_MAP ∪ {"code.commit"}`` → ``DANGEROUS``
   （收钱 / 外发 / 改权限 / 落库提交）;
2. ``namespace == "research"`` → ``EXTERNAL`` （联网）;
3. ``action ∈ {edit, write, save, generate, apply, commit}`` → ``WRITE``;
4. everything else (``inspect / query / score / profile / parse / render /
   diff / lint / run / execute``) → ``READ``.

Because :func:`classify_tool` is *total* (every string gets a class), it
trivially covers the whole ``PLATFORM_PLAN_TOOLS ∪ TOOL_PERMISSION_MAP`` set —
``tests/unit/test_inc46_tool_permissions.py`` pins that with no omissions.
"""

from __future__ import annotations

from typing import Any, Iterable

__all__ = [
    "TOOL_CLASSES",
    "READ",
    "WRITE",
    "EXTERNAL",
    "DANGEROUS",
    "CLASS_ORDER",
    "DANGEROUS_TOOLS",
    "classify_tool",
    "class_of_skill",
    "risk_level_from_classes",
]

#: The four privilege levels, least → most privileged (order is load-bearing:
#: the restricted sandbox gates on ``CLASS_ORDER``).
READ = "READ"
WRITE = "WRITE"
EXTERNAL = "EXTERNAL"
DANGEROUS = "DANGEROUS"
TOOL_CLASSES: tuple[str, ...] = (READ, WRITE, EXTERNAL, DANGEROUS)

#: Numeric privilege for one class; a higher value is more privileged.
CLASS_ORDER: dict[str, int] = {READ: 0, WRITE: 1, EXTERNAL: 2, DANGEROUS: 3}

#: Rule ③ — the action verbs that make a tool side-effecting (``WRITE``).
_WRITE_ACTIONS: frozenset[str] = frozenset(
    {"edit", "write", "save", "generate", "apply", "commit"}
)

#: Rule ① — the catalogue tool that is always ``DANGEROUS`` even though it is
#: **not** in ``TOOL_PERMISSION_MAP`` (its HITL gate is the handler's own
#: ``awaiting_approval``, not RBAC — see ``gate.py`` INC25 W2 note).
_FORCED_DANGEROUS: frozenset[str] = frozenset({"code.commit"})


def _dangerous_tools() -> frozenset[str]:
    """The ``DANGEROUS`` id set, **derived** from the runtime gate tool table.

    Union of every narrowly-granted tool (``TOOL_PERMISSION_MAP`` — money
    movement / data egress / privilege changes) and the explicitly forced id
    (``code.commit``). Imported lazily so this module never forms an import
    cycle with ``forgeflow.runtime``.
    """
    from forgeflow.runtime.gate import TOOL_PERMISSION_MAP

    return frozenset(TOOL_PERMISSION_MAP) | _FORCED_DANGEROUS


def DANGEROUS_TOOLS() -> frozenset[str]:  # noqa: N802 — kept as an explicit API name
    """Public accessor for the derived ``DANGEROUS`` id set (see §9-4).

    Exposed as a *function* (not a frozen module constant) so it always reads
    the current ``gate`` table — there is exactly one source of truth.
    """
    return _dangerous_tools()


def classify_tool(tool: str) -> str:
    """Return the privilege class of ``tool`` (``READ`` / ``WRITE`` / ``EXTERNAL``
    / ``DANGEROUS``).

    Applies the design's ordered rule set (§1.4). The function is *total*: any
    string — including the empty string and unknown names — resolves to a class
    (fail-closed to the least-privileged ``READ`` for the unrecognised tail).

    Args:
        tool: a dotted tool id (e.g. ``"research.search"``).

    Returns:
        One of :data:`TOOL_CLASSES`.
    """
    name = (tool or "").strip()
    if not name:
        return READ

    # Rule ① — narrowly-granted / forced-irreversible ids are DANGEROUS.
    if name in _dangerous_tools():
        return DANGEROUS

    namespace, _sep, action = name.partition(".")

    # Rule ② — the research namespace reaches the network.
    if namespace == "research":
        return EXTERNAL

    # Rule ③ — an explicit side-effecting verb.
    if action in _WRITE_ACTIONS:
        return WRITE

    # Rule ④ — everything else reads / computes.
    return READ


def _coerce_verification_classes(classes: Any) -> set[str]:
    """Normalise the ``classes`` argument of :func:`risk_level_from_classes`."""
    if isinstance(classes, dict):
        values: Iterable[Any] = classes.values()
    elif isinstance(classes, (list, tuple, set, frozenset)):
        values = classes
    else:
        values = [classes]
    return {str(v) for v in values if str(v) in TOOL_CLASSES}


def risk_level_from_classes(classes: Any) -> str:
    """Collapse a set of tool classes to a risk tier.

    ``DANGEROUS`` ⇒ ``high``; any ``WRITE`` / ``EXTERNAL`` ⇒ ``medium``; all
    ``READ`` (or nothing) ⇒ ``low``. Unknown / non-class values are ignored, so
    passing the whole :func:`class_of_skill` result (which also carries
    ``overall`` / ``risk_level``) is safe.

    Args:
        classes: a ``{tool: class}`` mapping, or a bare iterable of classes.

    Returns:
        One of ``"low"`` / ``"medium"`` / ``"high"``.
    """
    values = _coerce_verification_classes(classes)
    if DANGEROUS in values:
        return "high"
    if values & {WRITE, EXTERNAL}:
        return "medium"
    return "low"


def _overall_class(classes: Iterable[str]) -> str:
    """The most privileged class among ``classes`` (``READ`` when empty)."""
    values = [c for c in classes if c in CLASS_ORDER]
    if not values:
        return READ
    return max(values, key=lambda c: CLASS_ORDER[c])


def class_of_skill(spec: dict[str, Any] | None) -> dict[str, Any]:
    """Classify every tool a skill ``spec`` declares, plus an aggregate.

    Accepts the contract / draft-spec shape (a dict with a ``tools`` list) or
    any object exposing a ``tools`` attribute.

    Returns:
        A flat dict mapping each declared tool id → its class, **plus** two
        aggregate keys:

        * ``"overall"`` — the most privileged class across the tools
          (:data:`CLASS_ORDER`); ``"READ"`` when no tools are declared;
        * ``"risk_level"`` — :func:`risk_level_from_classes` over the mapping.

        (Matches the design §3.2 comment ``{tool: class, overall, risk_level}``.)
    """
    tools: Iterable[Any]
    if isinstance(spec, dict):
        tools = spec.get("tools", []) or []
    else:
        tools = getattr(spec, "tools", []) or []

    classes: dict[str, str] = {}
    for tool in tools:
        name = str(tool or "").strip()
        if name and name not in classes:
            classes[name] = classify_tool(name)

    result: dict[str, Any] = dict(classes)
    result["overall"] = _overall_class(classes.values())
    result["risk_level"] = risk_level_from_classes(classes)
    return result
