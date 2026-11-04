"""Pre-execution approval policy (8-feature program, Phase 4, B track).

Which tools must be approved BEFORE they execute (as opposed to the
A track, where a previewable artefact is staged and approved after it
is produced). The set is deliberately narrow — blanket approval is
the fatigue → rubber-stamp anti-pattern the research report warns
about — and it is a single source of truth, not a per-call decision:

* Every tool in :data:`forgeflow.runtime.gate.TOOL_PERMISSION_MAP`
  (the narrow-grant set: payment, export, grant, publish) requires
  pre-execution approval once bound. These tools are unbound today,
  and unbound tools fail closed to ``unavailable`` before the gate
  ever sees them — strictly stronger than gating, and the gate must
  not turn that into an approval round. The day one is bound, it is
  gated from its first call, never retrofitted after a first
  unapproved execution.
* An explicit registry covers future external tools and tests. It is
  additive only: registering a tool never removes the map set.

The gate itself lives in :mod:`forgeflow.runtime.tool_executor`
(the single choke point every tool call flows through); this module
only answers "does this tool need pre-approval" and "which argument
keys carry an approval" so the gate can hash the approved call
without the approval itself.
"""

from __future__ import annotations

from typing import Any

from forgeflow.runtime.gate import TOOL_PERMISSION_MAP

#: Tools that must be approved before execution, by construction.
PRE_APPROVAL_TOOLS: frozenset[str] = frozenset(TOOL_PERMISSION_MAP)

#: Argument keys that carry an approval. They are stripped before the
#: approved-call hash is computed, so the staged call (no approval
#: yet) and the resumed call (approval attached) hash identically.
APPROVAL_ARG_KEYS: frozenset[str] = frozenset({"pending_id", "approval"})

#: Explicit additions (future external tools, tests). Additive only.
_registry: set[str] = set()


def requires_pre_approval(tool: str) -> bool:
    """True when ``tool`` must be approved before it executes."""
    return tool in PRE_APPROVAL_TOOLS or tool in _registry


def register_pre_approval_tool(tool: str) -> None:
    """Add ``tool`` to the pre-approval set (tests, future tools)."""
    _registry.add(tool)


def unregister_pre_approval_tool(tool: str) -> None:
    """Remove a registry addition (test teardown). Never touches the map set."""
    _registry.discard(tool)


def strip_approval_args(args: dict[str, Any]) -> dict[str, Any]:
    """Copy ``args`` without approval-carrying keys (for hashing)."""
    return {key: value for key, value in args.items() if key not in APPROVAL_ARG_KEYS}


def hash_approval_args(args: dict[str, Any]) -> str:
    """Canonical sha256 of approval-stripped args.

    Single source of truth for the gate hash (Phase 4) and for the
    approver-edited args hash (Phase 9 A4): both must agree byte for
    byte, or an edited grant would never verify at execution time.
    """
    import hashlib
    import json

    staged = strip_approval_args(dict(args))
    try:
        canonical = json.dumps(staged, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        canonical = repr(staged)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
