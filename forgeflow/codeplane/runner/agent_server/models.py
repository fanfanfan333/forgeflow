"""Pydantic request/response models for the thin agent server (INC29 §4).

Field names mirror the official ``openhands-agent-server`` models so that
pointing the client at the official implementation changes only the base URL.

**No ``openhands`` import here** (and none at module scope anywhere in the
package): the models must load in BOTH virtualenvs — the ForgeFlow venv has no
OpenHands SDK. The SDK-typed request fields (``agent`` / ``workspace`` /
``initial_message``) are therefore declared as plain mappings and materialised
into real SDK objects inside ``conversation.py`` (which imports ``openhands``
lazily, inside functions).

This module adds no business vocabulary: it carries no ``label`` / ``phase`` /
``kind`` fields and no pass/fail fields — the test verdict is owned by ForgeFlow
(``forgeflow/codeplane/tests_verdict.py``).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Success",
    "SendMessageRequest",
    "StartConversationRequest",
    "ConversationInfo",
    "ConversationPage",
    "EventPage",
    "ExecuteBashRequest",
    "BashOutput",
    "GitDiff",
    "AgentResponseResult",
]


class Success(BaseModel):
    """The official generic acknowledgement body (``models.py::Success``)."""

    success: bool = True


class SendMessageRequest(BaseModel):
    """A message sent into a conversation (official ``SendMessageRequest``).

    ``content`` is passed through unchanged: the official type is a list of SDK
    content parts; here it is kept as an opaque value so this module stays free of
    SDK imports.
    """

    role: str = "user"
    content: Any = Field(default_factory=list)
    run: bool = False


class StartConversationRequest(BaseModel):
    """Body of ``POST /api/conversations`` (official ``StartConversationRequest``).

    The three official fields are declared verbatim (``agent`` / ``workspace`` /
    ``initial_message``); ``max_iterations`` is an additive, optional field the
    thin server reads to cap a run (the official server carries it on the
    conversation config) — it defaults to ``None`` = "leave the SDK default".
    """

    model_config = ConfigDict(extra="allow")

    agent: dict[str, Any] = Field(default_factory=dict)
    workspace: dict[str, Any] = Field(default_factory=dict)
    initial_message: SendMessageRequest | None = None
    max_iterations: int | None = None


class ConversationInfo(BaseModel):
    """Response body of the conversation endpoints (official ``ConversationInfo``).

    Field names and shapes follow the official model
    (``openhands/agent_server/models.py::ConversationInfo``): ``workspace`` carries
    its ``kind`` discriminator (``"LocalWorkspace"``) and ``agent`` echoes the raw
    agent spec the caller started the conversation with. Only the fields the
    ForgeFlow client actually reads are populated; the SDK-typed ``runtime_info``
    field of the official model is intentionally omitted (the client correlates by
    ``id`` + ``execution_status``).
    """

    id: str
    execution_status: str = "idle"
    workspace: dict[str, Any] = Field(default_factory=dict)
    agent: dict[str, Any] = Field(default_factory=dict)
    max_iterations: int = 500
    title: str | None = None
    created_at: str = ""
    updated_at: str = ""


class ConversationPage(BaseModel):
    """A page of conversations (official ``ConversationPage``).

    Paged with the official opaque cursor contract: ``next_page_id`` is echoed back
    by the client to fetch the following page.
    """

    items: list[ConversationInfo] = Field(default_factory=list)
    next_page_id: str | None = None


class EventPage(BaseModel):
    """A page of raw event frames (official ``EventPage`` shape).

    ``items`` carries the §2.1 raw frames verbatim — the server never adds the
    business ``label`` / ``phase`` / ``kind`` fields. ``next_page_id`` is the
    official opaque resume cursor (``openhands/agent_server/event_router.py::
    search_conversation_events``); the client echoes it back to page forward.
    """

    items: list[dict[str, Any]] = Field(default_factory=list)
    next_page_id: str | None = None


class ExecuteBashRequest(BaseModel):
    """Body of ``POST /api/bash/execute_bash_command`` (official model)."""

    command: str
    cwd: str | None = None
    timeout: int = 300


class BashOutput(BaseModel):
    """The raw output of a bash command (official ``BashOutput``).

    Carries only raw facts (``exit_code`` / ``stdout`` / ``stderr``). There is no
    pass/fail field: the verdict is derived by ForgeFlow from this raw output via
    ``tests_verdict.py::evaluate_test_output``.
    """

    id: str
    command_id: str
    order: int = 0
    exit_code: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    timeout: bool = False


class GitDiff(BaseModel):
    """A single-file diff (official ``GitDiff``): ``original`` vs ``modified``."""

    modified: str | None = None
    original: str | None = None


class AgentResponseResult(BaseModel):
    """The agent's final response (official ``AgentResponseResult``)."""

    response: str = ""
