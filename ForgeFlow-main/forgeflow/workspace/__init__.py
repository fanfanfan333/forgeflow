"""Workspace runtime — session / parent-run relationship facts (INC32).

The platform had no ``session_id`` / ``parent_run_id`` concept before INC32: hub
runs lived only in the process-lifetime ``MemoryRunStore``. This package adds the
**minimum** persisted projection that lets the workspace UI group runs into
conversations and lets a Follow-up reference its parent run across a restart —
see ``docs/sop/INC32-DESIGN.md`` ADR-02.

Only the run *header* + relationship + artifacts are persisted here; the run
*body* (steps / plan / observations / tool invocations) intentionally stays in
memory.
"""

from __future__ import annotations

from forgeflow.workspace.models import WorkspaceRunRecord
from forgeflow.workspace.store import (
    MemoryWorkspaceStore,
    PgWorkspaceStore,
    WorkspaceStore,
    clear_workspace_store,
    get_workspace_store,
    reset_workspace_store,
)

__all__ = [
    "WorkspaceRunRecord",
    "WorkspaceStore",
    "MemoryWorkspaceStore",
    "PgWorkspaceStore",
    "get_workspace_store",
    "reset_workspace_store",
    "clear_workspace_store",
]
