"""Thin OpenHands conversation wrapper → §2.1 raw frames (INC29 §4).

This module is the *only* place in the agent-server package that imports
``openhands``, and it does so **lazily, inside functions** — the package must
import in both virtualenvs, and the ForgeFlow venv has no SDK.

It is deliberately a thin relay:

  * it drives the SDK ``Conversation`` (``send_message`` + ``run``) and collects
    the SDK's own events;
  * it converts each SDK event into a **§2.1 raw frame** — ``type`` (the SDK class
    name) + a monotonically increasing ``seq`` + ``ts`` + passthrough raw fields;
  * it **never** computes ``label`` / ``phase`` / ``kind`` / business ``status``.
    That mapping is the sole responsibility of
    ``forgeflow/codeplane/events.py::adapt_openhands_event`` on the client side.

No ``forgeflow`` import. No ``@dataclass`` (plain classes with ``__slots__``): the
ForgeFlow suite loads some runner modules by path, and ``dataclasses`` resolves the
owning module through ``sys.modules`` — a path-loaded module is not there.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any

# The platform-context renderer is the single source of truth shared with the
# subprocess transport (``runner/context_block.py``). It is imported by relative
# name when this module is part of the ``forgeflow`` package (the tests, ``python
# -m ...``); when the server is launched as a bare script the package is imported as
# a top-level ``agent_server`` (no parent package), so fall back to the flat name —
# ``runner/`` is already on ``sys.path`` in that mode (see ``agent_server/__main__.py``).
# Two module objects, but the function is pure, so this is harmless.
try:  # pragma: no cover - the branch taken depends on how the server is launched
    from ..context_block import render_context_block
except ImportError:  # pragma: no cover
    from context_block import render_context_block

# The INC28 W1 action-policy guard lives beside this package in ``run_code_task`` —
# a sibling module that imports neither ``forgeflow`` nor ``openhands`` at module
# scope, so importing it is red-line-safe and SDK-free. Reusing the *same* guard is
# what makes the agent-server transport interchangeable with the subprocess one
# (INC29 T04 review D3): without it the Windows "absolute paths start with /"
# discipline silently disappears when the transport is switched. The symbol is only
# reused, never moved or duplicated.
try:  # pragma: no cover - the branch taken depends on how the server is launched
    from ..run_code_task import _install_file_editor_guard
except ImportError:  # pragma: no cover
    from run_code_task import _install_file_editor_guard

#: The code agent's minimal system prompt (self-contained). The server must not
#: import it from the runner (that would be a second source of truth); a caller
#: may override it via ``agent.system_prompt``.
_DEFAULT_SYSTEM_PROMPT = (
    "You are an autonomous software engineer working inside a sandboxed repository "
    "workspace.\n"
    "You accomplish tasks ONLY by calling tools. The `terminal` tool runs shell "
    "commands in the workspace; the `file_editor` tool reads and writes files in the "
    "workspace.\n"
    "Work on the concrete engineering task you were given immediately: read the "
    "relevant files, make the change, then verify it by running the project's tests. "
    "Never report success without evidence. Run non-interactive commands only.\n"
)

#: The terminal tool's no-output ("soft") timeout, in seconds (mirrors the runner's
#: INC25 P0-C knob so a slow-but-progressing command is not treated as stuck).
_TERMINAL_NO_CHANGE_TIMEOUT_S = 60


def _system_prompt_for(spec: dict[str, Any]) -> str:
    """The agent's system prompt: the minimal prompt with the platform context appended.

    Mirrors the subprocess transport exactly (INC29 T04 review D2): the rendered
    context block is **appended** to the minimal prompt (never substituted — the
    minimal prompt is load-bearing for the local model), and absent context adds no
    header. ``spec`` may override the base prompt via ``system_prompt``; the context
    is rendered from the ``skill_context`` / ``memory_context`` the client sent.
    """
    base = str(spec.get("system_prompt") or _DEFAULT_SYSTEM_PROMPT)
    return base + render_context_block(spec)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_int(value: Any, default: int = -1) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _iso(value: Any) -> str:
    if isinstance(value, str) and value:
        return value
    try:
        return value.isoformat()
    except Exception:  # noqa: BLE001 — an odd timestamp must never break a frame
        return _now()


def _stringify(value: Any) -> str:
    """Best-effort plain text of an SDK field (``""`` when there is none)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    for attr in ("text", "content", "message"):
        inner = getattr(value, attr, None)
        if isinstance(inner, str) and inner.strip():
            return inner.strip()
        if isinstance(inner, (list, tuple)):
            parts = [
                str(getattr(part, "text", part)).strip()
                for part in inner
                if str(getattr(part, "text", part) or "").strip()
            ]
            if parts:
                return "\n".join(parts)
    if isinstance(value, (list, tuple)):
        parts = [
            str(getattr(part, "text", part)).strip()
            for part in value
            if str(getattr(part, "text", part) or "").strip()
        ]
        return "\n".join(parts)
    return ""


def _event_text(event: Any) -> str:
    """The raw text an SDK event carries (never a business label)."""
    for attr in ("error", "tool_name", "action", "observation", "message", "content", "thought"):
        text = _stringify(getattr(event, attr, None))
        if text:
            return text
    return ""


def _fingerprint(event: Any) -> str:
    raw_id = getattr(event, "id", None)
    if isinstance(raw_id, str) and raw_id:
        return f"id:{raw_id}"
    return f"obj:{id(event)}"


def _frame_from_event(event: Any) -> dict[str, Any]:
    """Convert one SDK event into a §2.1 raw frame (no business fields)."""
    frame: dict[str, Any] = {"type": type(event).__name__, "ts": _iso(getattr(event, "timestamp", None))}
    source = getattr(event, "source", None)
    if source:
        frame["source"] = str(source)
    tool = getattr(event, "tool_name", None) or getattr(event, "tool", None)
    if tool:
        frame["tool"] = str(tool)
    detail = _event_text(event)
    if detail:
        frame["detail"] = detail
    observation = getattr(event, "observation", None)
    if observation is not None:
        for key in ("stdout", "stderr"):
            value = getattr(observation, key, None)
            if isinstance(value, str) and value:
                frame[key] = value
    return frame


def _workspace_dir_from(request: Any) -> str:
    workspace = getattr(request, "workspace", None)
    if isinstance(workspace, dict):
        for key in ("working_dir", "path", "dir"):
            value = workspace.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _message_text(message: Any) -> str:
    content = getattr(message, "content", None)
    text = _stringify(content)
    if text:
        return text
    return _stringify(getattr(message, "text", None))


def _build_workspace(workspace_dir: str) -> Any:
    """Build an SDK local workspace for ``workspace_dir`` (``None`` when unbuilt)."""
    if not workspace_dir:
        return None
    for module_name, attr in (
        ("openhands.sdk.workspace", "LocalWorkspace"),
        ("openhands.sdk", "LocalWorkspace"),
        ("openhands.workspace", "LocalWorkspace"),
    ):
        try:
            module = __import__(module_name, fromlist=[attr])
            workspace_cls = getattr(module, attr)
        except Exception:  # noqa: BLE001 — try the next known location
            continue
        for kwargs in ({"working_dir": workspace_dir}, {"path": workspace_dir}, {}):
            try:
                return workspace_cls(**kwargs)
            except TypeError:
                continue
            except Exception:  # noqa: BLE001
                break
    return None


def _build_tools(names: Any) -> list[Any]:
    """Materialise the agent's tool specs from tool names (best-effort)."""
    requested = [str(n) for n in names] if isinstance(names, (list, tuple)) else []
    if not requested:
        requested = ["terminal", "file_editor"]
    tool_cls = None
    for module_name in ("openhands.sdk", "openhands.tools"):
        try:
            tool_cls = getattr(__import__(module_name, fromlist=["Tool"]), "Tool")
            break
        except Exception:  # noqa: BLE001 — try the next known location
            continue
    if tool_cls is None:
        return []
    tools: list[Any] = []
    for name in requested:
        if name == "terminal":
            try:
                from openhands.tools.terminal import TerminalTool

                try:
                    tools.append(
                        tool_cls(
                            name=TerminalTool.name,
                            params={"no_change_timeout_seconds": _TERMINAL_NO_CHANGE_TIMEOUT_S},
                        )
                    )
                except Exception:  # noqa: BLE001 — fall back to the bare spec
                    tools.append(tool_cls(name=TerminalTool.name))
                continue
            except Exception:  # noqa: BLE001
                continue
        if name == "file_editor":
            try:
                from openhands.tools.file_editor import FileEditorTool

                tools.append(tool_cls(name=FileEditorTool.name))
                continue
            except Exception:  # noqa: BLE001
                continue
        try:
            tools.append(tool_cls(name=name))
        except Exception:  # noqa: BLE001 — an unusable spec is skipped, not fatal
            continue
    return tools


class ConversationRuntime:
    """One thin conversation: SDK lifecycle + the monotonic raw-frame buffer.

    The buffer is the source of truth for both ``GET .../events/search`` and the
    WebSocket: frames are appended once, assigned a monotonically increasing
    ``seq`` (from 0), and never rewritten. Duplicate SDK events (callbacks *and*
    the final ``state.events`` ledger) are de-duplicated by a stable fingerprint,
    so the stream is both loss-free and duplicate-free (A6).
    """

    __slots__ = (
        "conversation_id",
        "request",
        "workspace_dir",
        "max_iterations",
        "execution_status",
        "error",
        "created_at",
        "_conversation",
        "_frames",
        "_seen_ids",
        "_lock",
        "_thread",
        "_built",
        "_stopped",
    )

    def __init__(self, conversation_id: str, request: Any) -> None:
        self.conversation_id = str(conversation_id)
        self.request = request
        self.workspace_dir = _workspace_dir_from(request)
        raw_max = getattr(request, "max_iterations", None)
        self.max_iterations = _as_int(raw_max, 0)
        self.execution_status = "idle"
        self.error = ""
        self.created_at = _now()
        self._conversation: Any = None
        self._frames: list[dict[str, Any]] = []
        self._seen_ids: set[str] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._built = False
        self._stopped = False

    # ---------------------------------------------------------------- #
    # Raw-frame buffer                                                  #
    # ---------------------------------------------------------------- #
    def _append_locked(self, frame: dict[str, Any]) -> dict[str, Any]:
        frame = dict(frame)
        frame["seq"] = len(self._frames)
        self._frames.append(frame)
        return frame

    def _append_event(self, event: Any) -> None:
        if event is None or self._stopped:
            return
        fingerprint = _fingerprint(event)
        with self._lock:
            if fingerprint in self._seen_ids:
                return
            self._seen_ids.add(fingerprint)
            self._append_locked(_frame_from_event(event))

    def _append_error_frame(self, code: str, detail: str) -> None:
        """Relay a run failure as a raw error frame (an official event type).

        The server does not decide *what it means* — it emits the raw SDK event
        shape ``ConversationErrorEvent`` (the same class the SDK itself emits for
        ``MaxIterationsReached``) carrying the verbatim ``code`` (the exception
        class name) and ``detail`` (the exception repr). The client maps it.
        """
        with self._lock:
            self._append_locked(
                {
                    "type": "ConversationErrorEvent",
                    "ts": _now(),
                    "code": code,
                    "detail": detail,
                    "source": "environment",
                }
            )

    def _append_note_frame(self, detail: str) -> None:
        """Relay a non-fatal *note* as a visible raw frame (never silent).

        Used for honest notes that must not disappear — a system prompt the SDK
        rejected, an empty tool set, an action-policy guard that could not be
        seated (INC29 T04 review D3/D5). Emitted as the official ``AgentErrorEvent``
        shape, which ``events.py`` maps to one visible ``error`` step.

        Deliberately NOT a ``ConversationErrorEvent`` and deliberately carries **no**
        ``code``: the client derives a whole-run failure only from
        conversation/server-level errors, so a note can never masquerade as a
        transport death (a false attribution).
        """
        with self._lock:
            self._append_locked(
                {
                    "type": "AgentErrorEvent",
                    "ts": _now(),
                    "detail": str(detail),
                    "source": "environment",
                }
            )

    def frames_after(self, after_seq: int, limit: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = [dict(f) for f in self._frames if _as_int(f.get("seq")) > int(after_seq)]
        if limit is not None and limit >= 0:
            return items[:limit]
        return items

    def frames_from(
        self, start_seq: int, limit: int | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        """A page of frames with ``seq >= start_seq`` plus its opaque resume cursor.

        Implements the official ``GET .../events/search`` page contract
        (``openhands/agent_server/event_router.py::search_conversation_events``):
        ``start_seq`` is an **inclusive** lower bound (the value the client echoes
        back as ``page_id``), and the returned ``next_page_id`` is the next ``seq``
        to resume from — ``None`` when the caller has reached the end. Kept opaque to
        the client: the exact encoding is an implementation detail of this server.
        """
        with self._lock:
            total = len(self._frames)
            start = max(0, int(start_seq))
            page = [dict(f) for f in self._frames[start:]]
        if limit is not None and limit >= 0:
            page = page[:limit]
        next_start = start + len(page)
        next_page_id = str(next_start) if next_start < total else None
        return page, next_page_id

    def last_seq(self) -> int:
        with self._lock:
            return len(self._frames) - 1

    def event_count(self) -> int:
        with self._lock:
            return len(self._frames)

    def is_terminal(self) -> bool:
        return self.execution_status in ("finished", "error")

    # ---------------------------------------------------------------- #
    # Lifecycle                                                         #
    # ---------------------------------------------------------------- #
    def start(self) -> bool:
        """Build + drive the conversation on a daemon thread (``True`` if started)."""
        with self._lock:
            if self.execution_status != "idle":
                return False
            self.execution_status = "running"
        self._thread = threading.Thread(
            target=self._drive, name=f"agent-server-conv-{self.conversation_id[:8]}", daemon=True
        )
        self._thread.start()
        return True

    def shutdown(self) -> None:
        self._stopped = True

    def _drive(self) -> None:
        try:
            conversation = self._build()
            self._conversation = conversation
            conversation.run()
            for event in list(getattr(getattr(conversation, "state", None), "events", None) or []):
                self._append_event(event)
            self.execution_status = "finished"
        except Exception as exc:  # noqa: BLE001 — every failure is relayed verbatim
            self.error = repr(exc)
            self._append_error_frame(type(exc).__name__, repr(exc))
            self.execution_status = "error"

    def _build(self) -> Any:
        """Materialise the SDK ``Conversation`` (imports ``openhands`` lazily)."""
        if self._built and self._conversation is not None:
            return self._conversation
        from openhands.sdk import LLM, Agent, Conversation

        spec = self.request.agent if isinstance(getattr(self.request, "agent", None), dict) else {}
        llm_spec = spec.get("llm") if isinstance(spec.get("llm"), dict) else {}
        llm_kwargs: dict[str, Any] = {}
        for key in ("model", "base_url", "api_key", "reasoning_effort", "temperature", "litellm_extra_body"):
            if llm_spec.get(key) is not None:
                llm_kwargs[key] = llm_spec[key]
        llm = LLM(**llm_kwargs)

        tools = _build_tools(spec.get("tools"))
        if not tools:
            # Tools failing to materialise must be visible — never "runs but can do
            # nothing" (INC29 T04 review D5).
            self._append_note_frame("未能物化任何工具（tools 为空）——会话将在无工具下运行")

        system_prompt = _system_prompt_for(spec)
        try:
            agent = Agent(llm=llm, tools=tools, system_prompt=system_prompt)
        except TypeError:
            # The command-plane prompt is load-bearing; a silent fallback would
            # reproduce the INC25 failure mode where the 8B model degrades to
            # Q&A-ing. Report it verbatim (INC29 T04 review D5).
            agent = Agent(llm=llm, tools=tools)
            self._append_note_frame("自定义系统提示未被 SDK 接受（回退默认提示词）")

        workspace_obj = _build_workspace(self.workspace_dir)
        callbacks = [self._append_event]
        extra: dict[str, Any] = {}
        if self.max_iterations and self.max_iterations > 0:
            extra["max_iteration_per_run"] = int(self.max_iterations)

        attempts: list[dict[str, Any]] = []
        if workspace_obj is not None:
            attempts.append({"agent": agent, "workspace": workspace_obj, "callbacks": callbacks, **extra})
        attempts.append({"agent": agent, "callbacks": callbacks, **extra})

        conversation: Any = None
        last_error: Exception | None = None
        for kwargs in attempts:
            try:
                conversation = Conversation(**kwargs)
                break
            except TypeError as exc:
                last_error = exc
                continue
        if conversation is None:
            raise last_error or TypeError("Conversation(...) 不被当前 SDK 接受")

        # INC28 W1 / INC29 T04 D3 — seat the action-policy guard on the *resolved*
        # file_editor tool, exactly as the subprocess transport does. Without it the
        # agent-server transport silently lacks the Windows path discipline the
        # subprocess transport has, so the two are not interchangeable. A guard that
        # could not be seated is never silent.
        guard_note = _install_file_editor_guard(conversation, self.workspace_dir, None)
        if guard_note:
            self._append_note_frame(f"代码动作策略未启用（已降级）：{guard_note}")

        message = getattr(self.request, "initial_message", None)
        if message is not None:
            text = _message_text(message)
            if text:
                # Single positional argument only. The SDK's *second* positional
                # parameter is ``MessageEvent.sender`` (``str | None``) — passing a
                # run flag there raises a pydantic validation error, and the old
                # ``try/except TypeError`` masked exactly that (INC29 T04 review D4).
                # The run loop is started by the client via ``POST .../run``.
                conversation.send_message(text)

        self._built = True
        return conversation

    def info(self) -> Any:
        """Build the ``ConversationInfo`` response for this runtime.

        Mirrors the official ``ConversationInfo`` shape (INC30 T2): ``workspace``
        carries its ``kind`` discriminator (the class name the official
        ``LocalWorkspace`` exposes via ``DiscriminatedUnionMixin``), and ``agent``
        echoes back the raw agent spec the caller started the conversation with.
        Neither field is computed business state — they are passthrough facts.
        """
        from .models import ConversationInfo

        request_agent = getattr(self.request, "agent", None)
        agent_spec = dict(request_agent) if isinstance(request_agent, dict) else {}
        workspace: dict[str, Any] = {"kind": "LocalWorkspace"}
        if self.workspace_dir:
            workspace["working_dir"] = self.workspace_dir

        return ConversationInfo(
            id=self.conversation_id,
            execution_status=self.execution_status,
            workspace=workspace,
            agent=agent_spec,
            max_iterations=self.max_iterations or 500,
            created_at=self.created_at,
            updated_at=_now(),
        )


def new_conversation_id() -> str:
    """A fresh conversation id (dashed UUID, like the official server)."""
    return str(uuid.uuid4())
