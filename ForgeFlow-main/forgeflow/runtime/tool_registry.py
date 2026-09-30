"""Tool registry — maps plan tool ids to real handler bindings (INC12 A1).

A :class:`ToolBinding` names *what* implements a tool id, *whether* it is a real
implementation or a development stub, and which provider backs it. The registry
is a plain module-level ``dict`` (no singleton class) keyed by ``tool_id`` and
populated by :func:`load_default_bindings`.

Crucially, the five high-privilege tool ids (money movement / data egress /
privilege changes) are declared in :data:`UNBOUND_TOOLS` and are **never**
registered — so :func:`resolve` returns ``None`` for them and the executor
records them ``unavailable``. A tool that has no implementation must never be
reported as executed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

__all__ = [
    "ToolBinding",
    "UNBOUND_TOOLS",
    "register",
    "resolve",
    "known_ids",
    "reset_registry",
    "load_default_bindings",
]

#: Tool ids the platform deliberately does **not** bind to any implementation.
#: They require an explicit narrow grant (``runtime.gate.TOOL_PERMISSION_MAP``)
#: and, even if HITL were bypassed, execution must fail closed to
#: ``unavailable`` — never a fabricated ``ok``. Declared here so a test can
#: assert every one of them resolves to no handler.
UNBOUND_TOOLS: frozenset[str] = frozenset(
    {
        "payment.transfer",
        "payment.refund",
        "data.export",
        "policy.grant",
        "skill.publish",
    }
)


@dataclass(frozen=True)
class ToolBinding:
    """One tool id's implementation binding.

    Attributes:
        tool_id: the dotted plan tool id (e.g. ``"research.search"``).
        handler: ``async def handler(args, ctx) -> dict`` returning ``{"ok": ...}``.
        kind: ``"real"`` (a genuine implementation) or ``"development"`` (a
            development-only stub that must be refused outside ``dev``).
        provider: the concrete backing implementation label — e.g. ``"tavily"``,
            ``"development-stub"``, ``"stdlib-ast"``.
        description: human-readable summary for docs / diagnostics.
    """

    tool_id: str
    handler: Callable[[dict[str, Any], Any], Awaitable[dict[str, Any]]]
    kind: str
    provider: str
    description: str


#: Module-level registry — ``tool_id`` → :class:`ToolBinding`.
_REGISTRY: dict[str, ToolBinding] = {}


def register(binding: ToolBinding) -> ToolBinding:
    """Register (or replace) one binding by ``tool_id``. Returns the binding."""
    _REGISTRY[binding.tool_id] = binding
    return binding


def resolve(tool_id: str) -> ToolBinding | None:
    """Return the binding for ``tool_id``, or ``None`` when unbound.

    Lazily loads the default bindings on an empty registry so a caller never has
    to remember to call :func:`load_default_bindings` first. The unbound
    high-privilege ids always resolve to ``None``.
    """
    if not _REGISTRY:
        load_default_bindings()
    return _REGISTRY.get(tool_id)


def known_ids() -> frozenset[str]:
    """Every registered tool id."""
    return frozenset(_REGISTRY)


def reset_registry() -> None:
    """Drop every binding (test helper)."""
    _REGISTRY.clear()


def load_default_bindings() -> dict[str, ToolBinding]:
    """Register the platform's default tool bindings. **Idempotent.**

    Re-registering the same ``tool_id`` overwrites its single dict slot, so
    repeated calls never create duplicates (``known_ids()`` stays a set of the
    nine ids). Returns the registry for convenience.

    ``research.search``'s ``kind``/``provider`` reflect the *current* settings:
    a configured Tavily key makes it ``real``/``tavily``; otherwise it is a
    ``development``/``development-stub`` that a non-dev environment refuses.
    """
    from forgeflow.config import get_settings
    from forgeflow.runtime.tool_handlers import (
        analysis_profile,
        analysis_score,
        code_commit,
        code_execute,
        code_lint_handler,
        code_run,
        data_query,
        docs_parse,
        git_diff_handler,
        policy_check_handler,
        report_render,
        research_search,
    )

    try:
        tavily = bool(get_settings().is_tavily_enabled())
    except Exception:  # noqa: BLE001 — settings hiccup must not break startup
        tavily = False

    bindings = [
        ToolBinding(
            tool_id="research.search",
            handler=research_search,
            kind="real" if tavily else "development",
            provider="tavily" if tavily else "development-stub",
            description="Web research (Tavily when configured, else development stub)",
        ),
        ToolBinding(
            tool_id="data.query",
            handler=data_query,
            kind="development",
            provider="development-stub",
            description="Internal data query — permanently a development stub (no real warehouse)",
        ),
        ToolBinding(
            tool_id="policy.check",
            handler=policy_check_handler,
            kind="real",
            provider="local-engine",
            description="Governance verdict via the shipped PolicyEngine",
        ),
        ToolBinding(
            tool_id="git.diff",
            handler=git_diff_handler,
            kind="real",
            provider="git-cli",
            description="Read-only git diff via the git CLI",
        ),
        ToolBinding(
            tool_id="code.lint",
            handler=code_lint_handler,
            kind="real",
            provider="stdlib-ast",
            description="Deterministic static checks (stdlib ast + line length)",
        ),
        ToolBinding(
            tool_id="code.run",
            handler=code_run,
            kind="real",
            provider="stdlib-ast",
            description="Deterministic code validation (ast.parse + compile; never executes)",
        ),
        ToolBinding(
            tool_id="analysis.score",
            handler=analysis_score,
            kind="real",
            provider="stdlib-stats",
            description="Explainable score over this run's observations",
        ),
        # INC26 Q5 — the real, deterministic data-analysis step. Same shape as
        # ``analysis.score``: a genuine stdlib implementation (never a stub) that
        # reads a data file's real bytes and reports measured rows / aggregate.
        ToolBinding(
            tool_id="analysis.profile",
            handler=analysis_profile,
            kind="real",
            provider="stdlib-csv",
            description=(
                "Deterministic profile of a data file from its real bytes "
                "(stdlib csv): measured data-row count + column aggregate; "
                "unmeasured facts stay null"
            ),
        ),
        ToolBinding(
            tool_id="docs.parse",
            handler=docs_parse,
            kind="real",
            provider="stdlib-struct",
            description="Structured section parse of text (real line numbers)",
        ),
        ToolBinding(
            tool_id="report.render",
            handler=report_render,
            kind="real",
            provider="stdlib-render",
            description="Render this run's observations as Markdown",
        ),
        # INC25 W2 — the code-execution plane. ``kind="real"``: both delegate to
        # genuine implementations (the isolated-workspace engine / the guarded
        # workspace commit), never a development stub.
        ToolBinding(
            tool_id="code.execute",
            handler=code_execute,
            kind="real",
            provider="openhands-subprocess",
            description=(
                "Execute a code task in an isolated workspace via the OpenHands "
                "subprocess engine (outside the project tree; never writes the target repo)"
            ),
        ),
        ToolBinding(
            tool_id="code.commit",
            handler=code_commit,
            kind="real",
            provider="workspace-git",
            description=(
                "Human-in-the-loop gate over a code change: awaiting_approval until "
                "an ApprovalRecord is granted, then commit onto the workspace branch"
            ),
        ),
    ]
    for binding in bindings:
        register(binding)
    return dict(_REGISTRY)
