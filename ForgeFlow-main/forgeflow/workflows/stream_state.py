"""Rebuild the workflow's final state from an SSE update stream (BIZ-02).

``graph.astream`` yields **update patches**, not full states. Streaming
workflows used to drop those patches on the floor after forwarding them, so the
streaming entry point had no final state to persist — which is exactly how a
streamed run could finish while leaving no ``workflow_runs`` row and no approval
request behind.

:func:`merge_patch` applies the same reducer semantics as
:class:`forgeflow.state.workflow_state.WorkflowState`:

* ``messages`` uses ``add_messages`` (dedup by id) — the stream already strips it
  from the emitted patch, and persistence does not need it;
* ``research_results`` / ``analysis_scores`` / ``executed_actions`` / ``errors``
  use ``operator.add`` — a patch carries the **delta**, so the accumulator is
  extended, never replaced;
* every other channel is last-write-wins, so the patch overwrites.

The result is a partial-but-honest final state: it contains every field the
persistence path actually reads (stage, approval token, proposal, usage), and it
never fabricates a field the graph did not emit.
"""

from __future__ import annotations

from typing import Any

#: Channels declared with ``Annotated[..., operator.add]`` in ``WorkflowState``.
ADDITIVE_CHANNELS: frozenset[str] = frozenset(
    {"research_results", "analysis_scores", "executed_actions", "errors"}
)

#: Channels the SSE stream intentionally withholds (message objects are large and
#: not part of the persisted projection).
_EXCLUDED: frozenset[str] = frozenset({"messages"})


def merge_patch(accumulated: dict[str, Any], patch: dict[str, Any]) -> None:
    """Fold one node's update ``patch`` into ``accumulated`` in place."""
    for key, value in (patch or {}).items():
        if key in _EXCLUDED:
            continue
        if key in ADDITIVE_CHANNELS and isinstance(value, list):
            existing = accumulated.get(key)
            if isinstance(existing, list):
                accumulated[key] = [*existing, *value]
            else:
                accumulated[key] = list(value)
            continue
        accumulated[key] = value


__all__ = ["ADDITIVE_CHANNELS", "merge_patch"]
