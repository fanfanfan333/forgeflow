"""ForgeFlow code execution plane (INC25 W2).

The code plane lets a code task run inside an isolated workspace via the
OpenHands SDK, which lives in a **separate virtualenv** and is reached only over a
process boundary. This package is the ForgeFlow-side adapter:

  * :mod:`forgeflow.codeplane.protocol` — the JSONL wire contract;
  * :mod:`forgeflow.codeplane.workspace` — task-scoped isolated workspaces;
  * :mod:`forgeflow.codeplane.events` — raw event → standard event adapter;
  * :mod:`forgeflow.codeplane.engine` — the managed-subprocess engine;
  * :mod:`forgeflow.codeplane.tests_verdict` — the ForgeFlow-side test verdict;
  * :mod:`forgeflow.codeplane.approval` — the human-in-the-loop commit gate.

The ``runner/`` sub-package is a **standalone** program (it imports ``openhands``
and must never import ``forgeflow``); nothing here imports it — it is only ever
spawned as a subprocess.
"""

from __future__ import annotations

from forgeflow.codeplane.approval import (
    ACTION_APPROVED,
    ACTION_REANALYZED,
    ACTION_REJECTED,
    ACTION_REQUESTED,
    CODE_APPROVAL_KIND,
    decide_code_approval,
    find_code_approval,
    request_code_approval,
)
from forgeflow.codeplane.engine import (
    AgentServerOpenHandsEngine,
    CodeJob,
    CodePlaneEngine,
    CodeRunResult,
    SubprocessOpenHandsEngine,
    get_code_engine,
    reset_code_engine,
)
from forgeflow.codeplane.events import CodeEvent, adapt_openhands_event, summarize_timeline
from forgeflow.codeplane.protocol import DEGRADED_VALUES, STEP_STATUSES, decode_line, encode_line
from forgeflow.codeplane.tests_verdict import TestResult, evaluate_test_output
from forgeflow.codeplane.workspace import (
    Workspace,
    WorkspaceManager,
    get_workspace_manager,
    reset_workspace_manager,
)

__all__ = [
    "CodeJob",
    "CodeRunResult",
    "CodePlaneEngine",
    "SubprocessOpenHandsEngine",
    "AgentServerOpenHandsEngine",
    "get_code_engine",
    "reset_code_engine",
    "CodeEvent",
    "adapt_openhands_event",
    "summarize_timeline",
    "STEP_STATUSES",
    "DEGRADED_VALUES",
    "encode_line",
    "decode_line",
    "TestResult",
    "evaluate_test_output",
    "Workspace",
    "WorkspaceManager",
    "get_workspace_manager",
    "reset_workspace_manager",
    "CODE_APPROVAL_KIND",
    "ACTION_REQUESTED",
    "ACTION_APPROVED",
    "ACTION_REJECTED",
    "ACTION_REANALYZED",
    "request_code_approval",
    "find_code_approval",
    "decide_code_approval",
]
