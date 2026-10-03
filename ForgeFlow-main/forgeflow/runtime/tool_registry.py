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
    "register_mock_bindings",
    "snapshot_bindings",
    "restore_bindings",
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
        artifact_save,
        code_commit,
        code_execute,
        code_lint_handler,
        code_run,
        data_query,
        docs_parse,
        document_edit,
        document_inspect,
        git_diff_handler,
        pdf_generate,
        pdf_inspect,
        policy_check_handler,
        report_render,
        research_search,
        sheet_edit,
        sheet_inspect,
        textfile_edit,
        textfile_inspect,
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
        # INC43 S4 — the DOCX document-editing plane. All three are ``real``:
        # ``document.inspect`` / ``document.edit`` delegate to ``python-docx``
        # (a genuine OOXML read / write) and ``artifact.save`` is a stdlib
        # registration step over the run's own invocation trail — none is a
        # development stub, and none fabricates a result.
        ToolBinding(
            tool_id="document.inspect",
            handler=document_inspect,
            kind="real",
            provider="python-docx / python-pptx",
            description=(
                "Read a real Office document structure (DOCX via python-docx; "
                "PPTX via python-pptx; sniffed by OOXML package); never guesses"
            ),
        ),
        ToolBinding(
            tool_id="document.edit",
            handler=document_edit,
            kind="real",
            provider="python-docx / python-pptx",
            description=(
                "The document Tool layer: apply an explicit ``edits`` intent (or an "
                "intent resolved by the LLM) and REALLY write the new DOCX / PPTX bytes"
            ),
        ),
        ToolBinding(
            tool_id="artifact.save",
            handler=artifact_save,
            kind="real",
            provider="stdlib",
            description=(
                "Register this run's produced document / text deliverable (reads the "
                "run's own invocation trail); honest failure when there is none"
            ),
        ),
        # INC44 §1.3 — the text / code editing plane. Both are ``real`` stdlib
        # implementations: ``textfile.inspect`` measures the real line/encoding/EOL
        # structure and ``textfile.edit`` is the Tool layer that REALLY writes the
        # bytes (preserving EOL + encoding + BOM). Neither fabricates a result.
        ToolBinding(
            tool_id="textfile.inspect",
            handler=textfile_inspect,
            kind="real",
            provider="stdlib",
            description=(
                "Read a real text / code file structure (lines / chars / encoding / "
                "EOL / BOM / preview) with the standard library; never guesses"
            ),
        ),
        ToolBinding(
            tool_id="textfile.edit",
            handler=textfile_edit,
            kind="real",
            provider="stdlib",
            description=(
                "The text Tool layer: apply an explicit ``edits`` intent (or an "
                "intent resolved by the LLM) and REALLY write the new bytes "
                "(EOL / encoding / BOM preserving)"
            ),
        ),
        # INC45 §1.1/§1.2 — the XLSX edit plane and the PDF read/generate plane.
        # All four are ``real``: ``sheet.inspect`` / ``sheet.edit`` delegate to
        # ``openpyxl`` (a genuine OOXML read / write), ``pdf.inspect`` reuses the
        # existing ``pypdf`` extractor, and ``pdf.generate`` writes a real PDF via
        # ``fpdf2``. A missing optional extra degrades the step honestly
        # (``not_executed`` + verbatim) — never a fabricated result.
        ToolBinding(
            tool_id="sheet.inspect",
            handler=sheet_inspect,
            kind="real",
            provider="openpyxl",
            description=(
                "Read a real XLSX workbook structure (worksheets / rows / columns / "
                "header / cells) with openpyxl; missing extra degrades honestly"
            ),
        ),
        ToolBinding(
            tool_id="sheet.edit",
            handler=sheet_edit,
            kind="real",
            provider="openpyxl",
            description=(
                "The XLSX Tool layer: apply an explicit ``edits`` intent (or an "
                "intent resolved by the LLM) and REALLY write the new workbook bytes "
                "(formulas + styles preserved on untouched cells)"
            ),
        ),
        ToolBinding(
            tool_id="pdf.inspect",
            handler=pdf_inspect,
            kind="real",
            provider="pypdf",
            description=(
                "Read a real PDF's facts (page count / per-page chars / metadata / "
                "excerpt) by reusing multimodal.pdf.extract_pdf_text; never guesses"
            ),
        ),
        ToolBinding(
            tool_id="pdf.generate",
            handler=pdf_generate,
            kind="real",
            provider="fpdf2",
            description=(
                "Generate a NEW PDF from an explicit spec (or an LLM-resolved "
                "intent) via fpdf2; missing extra degrades honestly, never an empty PDF"
            ),
        ),
    ]
    for binding in bindings:
        register(binding)
    return dict(_REGISTRY)


# --------------------------------------------------------------------------- #
# INC46 T04 — sandbox mock provider.                                           #
#                                                                              #
# The skill sandbox ("restricted" tester mode) must be able to *simulate* a    #
# candidate skill's tool calls with **zero production side effects**. The       #
# mechanism is an alternative *provider* on the tool ids that already exist —   #
# never a new tool id. This keeps the orphan guard                           #
# ``set(PLATFORM_PLAN_TOOLS) == set(known_ids())`` intact (§9-4): a mock adds   #
# no catalogue entry and removes none, it only swaps an id's implementation.    #
# --------------------------------------------------------------------------- #

#: The provider label every sandbox mock binding carries.
MOCK_PROVIDER = "sandbox-mock"


def _make_mock_handler(tool_id: str) -> Callable[[dict[str, Any], Any], Awaitable[dict[str, Any]]]:
    """Build a deterministic, side-effect-free async handler for ``tool_id``.

    The handler performs no I/O, never calls a real implementation and
    fabricates nothing about the world: it simply echoes the request back with a
    ``mock`` marker. Its return shape matches every real handler
    (``{"ok": ...}``) so it is drop-in for the executor's await path.
    """

    async def _mock_handler(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        payload = dict(args) if isinstance(args, dict) else {"_args": repr(args)}
        return {
            "ok": True,
            "mock": True,
            "tool_id": tool_id,
            "provider": MOCK_PROVIDER,
            "echo": payload,
        }

    return _mock_handler


def register_mock_bindings() -> dict[str, ToolBinding]:
    """Replace every **already-registered** tool's provider with a sandbox mock.

    After the call every existing binding is ``kind="development"`` /
    ``provider="sandbox-mock"`` backed by :func:`_make_mock_handler` — a
    deterministic, side-effect-free stand-in. The mock never touches an external
    system, so a restricted-sandbox run can execute a plan without any production
    consequence.

    Guarantees (design §9-4):

    * **No new tool id.** Only ``tool_id`` values that are *already* in the
      registry are overwritten, so ``known_ids()`` is unchanged and the orphan
      guard ``set(PLATFORM_PLAN_TOOLS) == set(known_ids())`` still holds.
    * **Idempotent.** Re-calling overwrites each slot with an equivalent mock.
    * **Recoverable.** :func:`reset_registry` + :func:`load_default_bindings`
      restores the real providers.

    Returns:
        The registry (``tool_id`` → :class:`ToolBinding`), for convenience.
    """
    if not _REGISTRY:
        load_default_bindings()
    for tool_id in list(_REGISTRY):
        _REGISTRY[tool_id] = ToolBinding(
            tool_id=tool_id,
            handler=_make_mock_handler(tool_id),
            kind="development",
            provider=MOCK_PROVIDER,
            description=(
                f"Sandbox mock provider for '{tool_id}' "
                "(deterministic, no production side effects)"
            ),
        )
    return dict(_REGISTRY)


def snapshot_bindings() -> dict[str, ToolBinding]:
    """Return a shallow copy of the whole registry (``tool_id`` → binding).

    Pairs with :func:`restore_bindings` so a caller that *temporarily* swaps in
    mock providers (e.g. the restricted skill sandbox) can put every real
    handler back afterwards — the sandbox must never leave the process running
    on mocks. The copy holds the same immutable :class:`ToolBinding` objects, so
    restoring is exact.
    """
    return dict(_REGISTRY)


def restore_bindings(snapshot: dict[str, ToolBinding]) -> None:
    """Replace the whole registry with ``snapshot`` (inverse of a snapshot).

    Clears then re-populates in place so the module-level ``_REGISTRY`` object
    identity is preserved (callers holding a reference observe the restore). A
    ``None``/empty snapshot restores the empty state — a later :func:`resolve`
    still lazily rebuilds the defaults, so this can never wedge the registry.
    """
    _REGISTRY.clear()
    if snapshot:
        _REGISTRY.update(dict(snapshot))
