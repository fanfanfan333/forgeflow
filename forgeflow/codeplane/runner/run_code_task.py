#!/usr/bin/env python3
"""ForgeFlow code-task runner (INC25 W2) — **standalone**, openhands venv only.

This program is spawned by ``forgeflow/codeplane/engine.py`` with the OpenHands
virtualenv's interpreter. It reads ONE JSON job from stdin and writes a stream of
JSONL events to stdout, ending with a ``{"kind": "result", ...}`` line. It never
imports ``forgeflow`` and never writes anything outside the workspace directory it
is given.

Job (stdin)::

    {"run_id", "task_intent", "workspace_path", "model", "base_url", "api_key",
     "max_rounds", "wall_timeout_s", "test_command", "language_hint",
     "reasoning_effort", "num_ctx", "temperature"}

Event (stdout, one JSON object per line)::

    {"seq", "ts", "phase", "kind", "status", "tool?", "label", "detail?", "data?"}

Honesty rules:
  * the runner only ever reports **raw** output (stdout / exit code / events); the
    pass/fail verdict is ForgeFlow's job (``codeplane/tests_verdict.py``);
  * if the OpenHands SDK cannot be imported, or the model cannot be reached, the
    runner emits an explicit ``unavailable`` engine event with a **verbatim**
    reason instead of pretending to have run;
  * the environment it runs in has already had every ``*_PROXY`` variable removed
    by the parent engine (design §9), so the local-Ollama call is not proxied;
  * a run the runner deliberately **truncates** (round budget or wall clock) is
    reported with an explicit ``engine`` event carrying ``data.degraded``
    (``max_rounds`` / ``timeout``) — never as a silent success.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

# The shared, pure context renderer lives beside this script (``context_block.py``).
# When this file is launched as a script, ``sys.path[0]`` is already ``runner/``;
# when it is loaded by path (the offline unit tests) it is not, so make the sibling
# importable explicitly. Keeping exactly ONE renderer, shared with the agent-server
# transport, is what stops the two transports from injecting different context
# while the platform still reports the same ``codeplane.injected`` (T04 review D2).
_RUNNER_DIR = os.path.dirname(os.path.abspath(__file__))
if _RUNNER_DIR not in sys.path:
    sys.path.insert(0, _RUNNER_DIR)

from context_block import render_context_block  # noqa: E402 — path bootstrapped above

# Timeout for the in-workspace test command, independent of the engine's overall
# wall clock; kept module-level so it is trivially inspectable.
_TEST_TIMEOUT_SECONDS = 300

#: The terminal tool's no-output ("soft") timeout, in seconds. The SDK default is
#: 10s, which is short enough that a slow-but-progressing command (``pip install``,
#: a cold test collection) trips it, hands control back with exit code -1, and
#: (empirically) derails the local model into retrying the same command. We raise
#: it so a genuinely working command is allowed to make progress. It is threaded
#: through ``Tool(name=..., params={"no_change_timeout_seconds": ...})`` — the SDK
#: resolves params with ``TerminalTool.create(conv_state=..., **params)`` (verified
#: in ``openhands/sdk/tool/registry.py::resolve_tool``).
_TERMINAL_NO_CHANGE_TIMEOUT_S = 60

#: Seconds of the engine's wall clock the runner keeps in reserve so it can still
#: emit the diff, the (skipped) test note and the terminal ``result`` line *before*
#: the engine's own ``asyncio.wait_for`` fires. Without it the runner and the engine
#: share the exact same ceiling and the engine usually kills the process first —
#: losing the runner's honest ``timeout`` degrade and its diff. The conversation is
#: therefore capped at ``wall_timeout_s - _WRAP_UP_RESERVE_SECONDS``.
_WRAP_UP_RESERVE_SECONDS = 20


class _MaxRoundsExceeded(Exception):
    """Raised when the agent consumed its whole round budget without finishing."""

    def __init__(self, max_rounds: int, actions: int) -> None:
        self.max_rounds = max_rounds
        self.actions = actions
        super().__init__(
            f"已达最大回合上限（max_rounds={max_rounds}，已执行 {actions} 个动作），已中止"
        )


class _WallClockExceeded(Exception):
    """Raised when the whole conversation exceeds the wall-clock ceiling."""

    def __init__(self, wall_timeout_s: int) -> None:
        self.wall_timeout_s = wall_timeout_s
        super().__init__(
            f"代码任务整段对话超过墙钟上限 {wall_timeout_s}s，已中止（timeout）"
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Emitter:
    """Writes the JSONL event stream to a dedicated stream (one flushed line/event).

    The stream is captured **once at construction** (defaulting to ``sys.stdout``)
    rather than looked up per write. The runner redirects the *process* stdout to
    stderr while the OpenHands SDK runs — the SDK's rich console would otherwise
    interleave its human output into the JSONL, and the engine reads every such
    line as unparseable and spuriously degrades the whole run to
    ``parse_failed``. The emitter must therefore keep writing to the real stdout.
    """

    def __init__(self, stream=None) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self.seq = 0

    def emit(
        self,
        phase: str,
        kind: str,
        status: str,
        label: str,
        *,
        tool: str | None = None,
        detail: str = "",
        data: dict | None = None,
        latency_ms: float | None = None,
    ) -> None:
        event: dict = {
            "seq": self.seq,
            "ts": _now(),
            "phase": phase,
            "kind": kind,
            "status": status,
            "label": label,
        }
        if tool:
            event["tool"] = tool
        if detail:
            event["detail"] = detail
        if latency_ms is not None:
            event["latency_ms"] = latency_ms
        if data is not None:
            event["data"] = data
        self._stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._stream.flush()
        self.seq += 1


def _read_job() -> dict:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        parsed = json.loads(raw) if raw.strip() else {}
    except (TypeError, ValueError):
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def _classify_conversation_error(exc: BaseException) -> tuple[str, str]:
    """Map a conversation failure onto a ``codeplane.degraded`` value + reason.

    A transport / model failure (Ollama down, LiteLLM connect error) is a
    ``model_unavailable``; anything else is an ``engine_unavailable``. The raw
    repr is returned unharmed as the reason so it can travel to the Trace/title.
    """
    text = repr(exc)
    lowered = text.lower()
    model_hints = (
        "model", "ollama", "litellm", "api key", "connection", "connect",
        "refused", "timed out", "timeout", "network", "api_base", "11434",
    )
    if any(hint in lowered for hint in model_hints):
        return "model_unavailable", text
    return "engine_unavailable", text


def _make_workspace(workspace_path: str):
    """Build an OpenHands local workspace for ``workspace_path`` (best-effort)."""
    candidates = (
        ("openhands.sdk.workspace", "LocalWorkspace"),
        ("openhands.sdk", "LocalWorkspace"),
        ("openhands.workspace", "LocalWorkspace"),
    )
    for module_name, attr in candidates:
        try:
            module = __import__(module_name, fromlist=[attr])
            workspace_cls = getattr(module, attr)
        except Exception:  # noqa: BLE001 — try the next known location
            continue
        for kwargs in ({"working_dir": workspace_path}, {"path": workspace_path}, {}):
            try:
                return workspace_cls(**kwargs)
            except TypeError:
                continue
            except Exception:  # noqa: BLE001
                break
    return None


def _default_tools() -> list:
    """OpenHands default tool set, browser tools disabled (headless host).

    Retained for reference only — the code agent no longer uses it. The SDK
    default preset also enables ``task_tracker`` (see ``_explicit_tools``), which
    empirically derails the local qwen model, so :func:`_explicit_tools` is what
    the production path constructs.
    """
    try:
        from openhands.tools.preset.default import get_default_tools

        try:
            return list(get_default_tools(enable_browser=False))
        except TypeError:
            return list(get_default_tools())
    except Exception:  # noqa: BLE001 — a tools-less agent is still runnable
        return []


#: The code agent's system prompt — passed verbatim to ``Agent(system_prompt=...)``.
#: Without it the agent inherits OpenHands' built-in prompt, which (empirically)
#: makes the local qwen model treat the task as a Q&A about the project instead of
#: acting. Kept module-level so a counterfactual test can blank it.
_SYSTEM_PROMPT = (
    "You are an autonomous software engineer working inside a sandboxed repository "
    "workspace on the user's machine.\n"
    "You accomplish tasks ONLY by calling tools. The `terminal` tool runs shell "
    "commands in the workspace; the `file_editor` tool reads and writes files in the "
    "workspace.\n"
    "Rules:\n"
    "1. Never answer questions about your tools or capabilities. The user has already "
    "given you a concrete engineering task; start working on it immediately.\n"
    "2. Read the relevant files before editing them.\n"
    "3. Make the actual code change with `file_editor`.\n"
    "4. Verify the change by running the project's tests with `terminal`.\n"
    "5. Only call `finish` once the change has been made AND verified. Never report "
    "success without evidence.\n"
    "6. Run non-interactive commands only. Never launch a command that waits for "
    "keyboard input or opens an editor/mini-buffer (`git commit` without `-m`, "
    "`git rebase -i`, `pytest --pdb`, a bare `python`/`pip` prompt, `less`, `vim`, "
    "`nano`, `more`). If a command returns `exit code -1` (it hit the tool's soft "
    "timeout and is still running), do NOT re-run the same command: either wait for "
    "it to finish, or stop it and switch to a bounded, non-interactive form "
    "(e.g. add `--no-pager`, `-y`, `-q`, or pipe input). Repeatedly retrying a stuck "
    "interactive command is a failure mode — never do it.\n"
)


def _context_block(job: dict) -> str:
    """Render the enterprise context ForgeFlow selected for this run (INC27).

    Thin, single-source delegate to
    :func:`forgeflow.codeplane.runner.context_block.render_context_block` — the very
    same renderer the agent-server transport uses, so both transports deliver the
    identical selected subset, byte-for-byte (INC29 T04 review D2). See that module
    for the "nothing injected ⇒ nothing rendered" / "appended, never substituted"
    contract.
    """
    return render_context_block(job)


# ---------------------------------------------------------------------------
# INC27 — the code-action policy layer
# ---------------------------------------------------------------------------
# Architecture: the model proposes an Action, ForgeFlow decides, OpenHands
# executes. The decision is not one big "is this allowed?" — the platform has to
# understand **which kind** of failure it is looking at and react differently:
#
#     lossless format problem  -> normalize, then execute
#     factual error            -> reject + observation, let the agent retry
#     boundary / permission    -> hard reject, audit
#
# This taxonomy is the whole point: an agent that is not always right is fine as
# long as the platform can tell "fix it", "make it look again" and "stop" apart.
#
# The rules are a **table**, not a chain of hard-coded ``if``s: today the runner
# constructs them locally, and when the control plane starts issuing a
# ``code_policy`` the only change is where this list comes from.

#: Verdicts a rule can return.
_POLICY_ALLOW = "allow"
_POLICY_NORMALIZE = "normalize"
_POLICY_REJECT_RETRYABLE = "reject_retryable"
_POLICY_REJECT_SECURITY = "reject_security"

#: The two error classes a rejection belongs to — reported verbatim to the agent.
_RETRYABLE_KIND = "retryable"
_SECURITY_KIND = "security"


class PolicyDecision:
    """One rule's verdict: what to do, with which action, said how.

    Deliberately **not** a ``@dataclass``: this module is loaded by path (see the
    ForgeFlow unit tests) and ``dataclasses`` resolves the owning module through
    ``sys.modules``, which a path-loaded module is not in.
    """

    __slots__ = ("verdict", "action", "message")

    def __init__(self, verdict: str, action: object | None = None, message: str = "") -> None:
        self.verdict = verdict
        self.action = action
        self.message = message

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"PolicyDecision({self.verdict!r}, {self.action!r}, {self.message!r})"


class _PolicyRule:
    """Base rule. ``kind`` is the error class reported when the rule fires."""

    name = "rule"
    kind = ""

    def __init__(self, workspace_root: str = "") -> None:
        self.workspace_root = workspace_root

    def evaluate(self, action: object) -> PolicyDecision:  # pragma: no cover - abstract
        raise NotImplementedError


#: A POSIX-trained model told "absolute paths start with /" while its workspace
#: is ``D:/work`` writes ``/D:/work/a.py``. On Windows that leading slash eats
#: the drive letter: ``Path`` reads it as a drive-less root and the editor
#: rejects it as "not absolute". Dropping the slash preserves every bit of
#: intent, so this is a *normalization*, not a guess.
_WINDOWS_DRIVE_PREFIX_RE = re.compile(r"^/+(?=[A-Za-z]:[\\/])")

#: A UNC path (``//host/share`` / ``\\\\host\\share``) is already host-absolute;
#: it must never be read as "workspace-root relative".
_UNC_PREFIX_RE = re.compile(r"^[\\/]{2,}")

#: ``D:`` / ``d:`` — a real Windows drive letter at the start of the string.
#: Checked after ``_WINDOWS_DRIVE_PREFIX_RE`` so ``/D:/x`` is handled there.
_WINDOWS_DRIVE_LETTER_RE = re.compile(r"^[A-Za-z]:")


def _normalize_host_path(path: str, workspace_root: str = "") -> str:
    """Return ``path`` in the form this host's filesystem accepts (lossless).

    Two **intent-preserving** rewrites, both Windows-only:

    * ``/D:/work/a.py`` -> ``D:/work/a.py`` — a leading slash in front of a real
      drive letter is stripped (the drive letter is the intent);
    * ``/billing/__init__.py`` -> ``<workspace_root>\\billing\\__init__.py`` — a
      leading slash/backslash, **drive-less**, non-UNC path is the same class of
      repair: the model expressed a real intent (this file, inside the workspace)
      using a POSIX-absolute spelling the host rejects. It is anchored to the
      workspace root, which is *recovery of intent*, **not** a permission
      decision — the rewritten path is still handed to ``workspace_boundary``, so
      a genuine escape is still stopped.

    ``workspace_root`` is **optional**: when it is empty (every pre-INC28 caller,
    e.g. the rule-table probes) the behaviour is byte-identical to before — only
    the drive-prefix rewrite applies.
    """
    text = (path or "").strip()
    if not text or os.name != "nt":
        return text
    match = _WINDOWS_DRIVE_PREFIX_RE.match(text)
    if match:
        return text[match.end():]
    # Leading slash/backslash, no drive letter, not UNC ⇒ workspace-relative
    # intent. Needs a root to anchor to; without one, leave the text untouched.
    if (
        workspace_root
        and text[0] in ("/", "\\")
        and not _UNC_PREFIX_RE.match(text)
        and not _WINDOWS_DRIVE_LETTER_RE.match(text)
    ):
        parts = [part for part in re.split(r"[\\/]+", text.lstrip("/\\")) if part]
        if parts:
            return os.path.join(workspace_root, *parts)
    return text


class _PathFormatRule(_PolicyRule):
    """Host path spelling: ``/D:/x`` -> ``D:/x``, ``/x`` -> ``<ws>/x``.

    Both are lossless-in-intent host-spelling repairs, so they are corrected
    (``normalize``) rather than rejected. The rule still runs **before**
    ``workspace_boundary``, so the corrected path must clear the boundary check.
    """

    name = "path_format"
    kind = "normalize"

    def evaluate(self, action: object) -> PolicyDecision:
        path = getattr(action, "path", "") or ""
        fixed = _normalize_host_path(path, self.workspace_root)
        if fixed == path:
            return PolicyDecision(_POLICY_ALLOW)
        try:
            corrected = action.model_copy(update={"path": fixed})
        except Exception:  # noqa: BLE001 - an uncopyable action is left to the SDK
            return PolicyDecision(_POLICY_ALLOW)
        return PolicyDecision(
            _POLICY_NORMALIZE, corrected, f"path normalized: {path} -> {fixed}"
        )


class _WorkspaceBoundaryRule(_PolicyRule):
    """Workspace containment. A violation is never auto-corrected — it is a stop."""

    name = "workspace_boundary"
    kind = _SECURITY_KIND

    def __init__(self, workspace_root: str = "") -> None:
        super().__init__(workspace_root)
        self._root = os.path.realpath(os.path.abspath(workspace_root or os.getcwd()))

    def _inside(self, path: str) -> bool:
        try:
            target = os.path.realpath(os.path.abspath(path))
        except Exception:  # noqa: BLE001 - an unresolvable path is not inside
            return False
        return target == self._root or target.startswith(self._root + os.sep)

    def evaluate(self, action: object) -> PolicyDecision:
        path = (getattr(action, "path", "") or "").strip()
        if not path:
            return PolicyDecision(_POLICY_REJECT_SECURITY, message="action carries no path")
        if not self._inside(path):
            return PolicyDecision(
                _POLICY_REJECT_SECURITY, message=f"path escapes the workspace: {path}"
            )
        return PolicyDecision(_POLICY_ALLOW)


#: Commands that only make sense on a file that is already there.
_MUST_EXIST_COMMANDS = ("view", "str_replace", "insert", "undo_edit")


class _FileExistsRule(_PolicyRule):
    """Existence preconditions. The SDK also checks these, but its message is not
    actionable; ours tells the agent what to do next."""

    name = "file_exists"
    kind = _RETRYABLE_KIND

    def evaluate(self, action: object) -> PolicyDecision:
        command = getattr(action, "command", "") or ""
        path = getattr(action, "path", "") or ""
        if not path:
            return PolicyDecision(_POLICY_ALLOW)
        exists = os.path.exists(path)
        if command in _MUST_EXIST_COMMANDS and not exists:
            return PolicyDecision(
                _POLICY_REJECT_RETRYABLE,
                message=(
                    f"target file does not exist: {path}\n"
                    "Required next action: view the workspace directory and use the "
                    "real path."
                ),
            )
        if command == "create" and exists:
            return PolicyDecision(
                _POLICY_REJECT_RETRYABLE,
                message=(
                    f"target file already exists: {path}\n"
                    "Required next action: use `str_replace` or `insert` instead of "
                    "`create`."
                ),
            )
        return PolicyDecision(_POLICY_ALLOW)


class _OldStrMatchRule(_PolicyRule):
    """``str_replace`` must be grounded in the file's real bytes.

    This is the rule that turns an unverifiable model behaviour (an 8B model
    inventing an ``old_str`` without ever reading the file) into a mechanically
    checkable contract. The platform never guesses a replacement string — it
    refuses and sends the agent back to look.
    """

    name = "old_str_match"
    kind = _RETRYABLE_KIND

    def evaluate(self, action: object) -> PolicyDecision:
        command = getattr(action, "command", "") or ""
        if command != "str_replace":
            return PolicyDecision(_POLICY_ALLOW)
        old_str = getattr(action, "old_str", None)
        if old_str is None or not str(old_str).strip():
            return PolicyDecision(
                _POLICY_REJECT_RETRYABLE,
                message=(
                    "`old_str` is empty.\n"
                    "Required next action: view the file, then set `old_str` to the "
                    "exact text to replace."
                ),
            )
        path = getattr(action, "path", "") or ""
        try:
            content = pathlib.Path(path).read_text(encoding="utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 - an unreadable file is a factual error
            return PolicyDecision(
                _POLICY_REJECT_RETRYABLE,
                message=(
                    f"cannot read target file: {path} ({exc.__class__.__name__})\n"
                    "Required next action: view the file first."
                ),
            )
        occurrences = content.count(str(old_str))
        if occurrences == 0:
            return PolicyDecision(
                _POLICY_REJECT_RETRYABLE,
                message=(
                    f"old_str was not found in {path}\n"
                    f"Required next action: view {path} and copy the text verbatim, "
                    "then edit again."
                ),
            )
        if occurrences > 1:
            return PolicyDecision(
                _POLICY_REJECT_RETRYABLE,
                message=(
                    f"old_str matches {occurrences} locations in {path}\n"
                    "Required next action: include more surrounding lines so it is "
                    "unique."
                ),
            )
        return PolicyDecision(_POLICY_ALLOW)


#: The rule table. Order is the evaluation order; a rule that normalizes hands
#: its corrected action to the next rule, so later rules always see the path the
#: executor would really receive.
_POLICY_RULE_TYPES = (
    _PathFormatRule,
    _WorkspaceBoundaryRule,
    _FileExistsRule,
    _OldStrMatchRule,
)


def _build_policy_rules(workspace_root: str) -> list[_PolicyRule]:
    """Instantiate the rule table for one workspace."""
    return [rule_cls(workspace_root) for rule_cls in _POLICY_RULE_TYPES]


def _policy_rejection_text(rule_name: str, kind: str, message: str) -> str:
    """The observation text for a rejected action (structured, not a hint)."""
    return (
        "ACTION_REJECTED\n\n"
        f"Rule:\n{rule_name} ({kind})\n\n"
        f"Reason:\n{message}\n"
    )


class PolicyRejection(Exception):
    """A rejected action when no observation builder is installed (test seam)."""

    def __init__(self, rule: str, kind: str, message: str) -> None:
        self.rule = rule
        self.kind = kind
        self.message = message
        super().__init__(_policy_rejection_text(rule, kind, message))


class _PolicyGuardedExecutor:
    """Policy table -> real executor: every action crosses the table first.

    Deliberately free of any ``openhands`` import, so the rules can be exercised
    directly (the runner is standalone; the ForgeFlow suite loads it *without* the
    SDK on the path). The SDK-typed subclass is built in
    :func:`_install_file_editor_guard`.
    """

    def __init__(
        self,
        inner: object,
        rules,
        workspace_root: str = "",
        emitter: "Emitter | None" = None,
        observation_builder=None,
    ) -> None:
        self._inner = inner
        self._rules = list(rules or [])
        self._workspace_root = workspace_root
        self._emitter = emitter
        self._observation_builder = observation_builder

    @property
    def rule_names(self) -> list[str]:
        return [rule.name for rule in self._rules]

    def _note(self, status: str, label: str, detail: str, rule: str, verdict: str) -> None:
        """Emit one policy event, so the run's timeline shows what the platform did."""
        if self._emitter is None:
            return
        self._emitter.emit(
            "action", "policy", status, label,
            tool="file_editor", detail=detail,
            data={"policy": rule, "verdict": verdict},
        )

    def __call__(self, action, conversation=None):
        current = action
        for rule in self._rules:
            decision = rule.evaluate(current)
            if decision.verdict == _POLICY_ALLOW:
                continue
            if decision.verdict == _POLICY_NORMALIZE:
                self._note(
                    "ok", f"策略已纠正动作参数（{rule.name}）", decision.message,
                    rule.name, decision.verdict,
                )
                current = decision.action
                continue
            kind = (
                _RETRYABLE_KIND
                if decision.verdict == _POLICY_REJECT_RETRYABLE
                else _SECURITY_KIND
            )
            text = _policy_rejection_text(rule.name, kind, decision.message)
            self._note(
                "error", f"动作被策略拒绝（{rule.name}）", text,
                rule.name, decision.verdict,
            )
            if self._observation_builder is None:
                raise PolicyRejection(rule.name, kind, decision.message)
            return self._observation_builder(current, text)
        return self._inner(current, conversation)


#: The SDK-typed guarded executor class, built once per process. ``ToolExecutor``
#: is imported lazily so this module stays stdlib-only at import time (the
#: ForgeFlow suite loads it *without* the SDK on the path).
_GUARDED_EXECUTOR_CLS: type | None = None


def _guarded_executor_class() -> type:
    """Return (building once) the SDK-typed policy-guarded executor class."""
    global _GUARDED_EXECUTOR_CLS
    if _GUARDED_EXECUTOR_CLS is not None:
        return _GUARDED_EXECUTOR_CLS

    from openhands.sdk.tool.tool import ToolExecutor

    class _GuardedFileEditorExecutor(_PolicyGuardedExecutor, ToolExecutor):
        """SDK-typed executor whose concrete ``__call__`` satisfies the ABC.

        Order matters: our class must come FIRST in the MRO, otherwise
        ``ABCMeta`` resolves ``__call__`` to ``ToolExecutor``'s abstract
        declaration and refuses to instantiate the class.
        """

    _GUARDED_EXECUTOR_CLS = _GuardedFileEditorExecutor
    return _GUARDED_EXECUTOR_CLS


def _guarded_observation_builder(action, text: str):
    """Build the SDK ``FileEditorObservation`` that carries a rejection back."""
    from openhands.tools.file_editor.definition import FileEditorObservation

    return FileEditorObservation.from_text(
        text,
        is_error=True,
        command=getattr(action, "command", "view"),
        path=getattr(action, "path", None),
        prev_exist=os.path.exists(getattr(action, "path", "") or ""),
    )


def _install_file_editor_guard(
    conversation, workspace_path: str, emitter: "Emitter | None" = None
) -> str:
    """Seat the policy guard on the SDK's **resolved** ``file_editor`` tool.

    INC28 W1 — the original seam patched ``FileEditorTool.create`` (a classmethod),
    but the SDK's ``resolve_tool`` looks the tool up through a resolver that
    captured ``create`` **at import time**
    (``openhands/sdk/tool/registry.py``::``_resolver_from_subclass``), so patching
    the class attribute afterwards had **no effect**. This was proved with a
    counterfactual probe (``_audit/inc28-eng/probe_w1_seam.py``): the registered
    resolver answered ``patched create called? False``.

    The seam the SDK actually uses at call time is
    ``ToolDefinition.__call__`` → ``self.executor(action, conversation)``
    (``openhands/sdk/agent/agent.py``::``_execute_action_event`` reads
    ``self.tools_map`` → ``self._tools``). So once the Agent has resolved its
    tools we walk the **real, instantiated** tool map and ``set_executor`` on the
    file editor. No SDK path validation is relaxed anywhere.

    Returns ``""`` when the guard is fully active; anything else is the verbatim
    reason it is not — a missing guard is never silent (the caller emits it).
    """
    try:
        return _install_file_editor_guard_inner(conversation, workspace_path, emitter)
    except Exception as exc:  # noqa: BLE001 — reported verbatim, never swallowed
        return f"挂载动作策略守卫失败：{exc!r}"


def _install_file_editor_guard_inner(
    conversation, workspace_path: str, emitter: "Emitter | None" = None
) -> str:
    """Body of :func:`_install_file_editor_guard` (returns ``""`` on success)."""
    if not workspace_path:
        return "未提供工作区根，动作策略守卫无处锚定"

    # Materialise the agent's tools now (idempotent). Without it the resolved tool
    # map may still be empty and the guard would silently miss its target.
    ensure = getattr(conversation, "_ensure_agent_ready", None)
    if callable(ensure):
        ensure()

    agent = getattr(conversation, "agent", None)
    tools_map = getattr(agent, "tools_map", None)
    if not isinstance(tools_map, dict) or not tools_map:
        return "会话未暴露已解析的 agent 工具表（tools_map 为空）"
    tool = tools_map.get("file_editor")
    if tool is None:
        return f"agent 工具表中没有 file_editor（现有：{sorted(tools_map)}）"
    inner = getattr(tool, "executor", None)
    if inner is None:
        return "file_editor 工具没有可包装的执行器"

    rules = _build_policy_rules(workspace_path)
    guarded = tool.set_executor(
        _guarded_executor_class()(
            inner, rules, workspace_path, emitter, _guarded_observation_builder
        )
    )

    # INC28 — also correct the SDK's "absolute paths start with /" sentence *in the
    # live description* so the model is steered to a host-valid spelling. Same
    # repair as the original seam attempted; it now actually reaches the tool the
    # LLM is shown (``Agent`` reads ``tools_map`` per step).
    description, replaced = _host_path_line_fix(getattr(guarded, "description", ""))
    note = ""
    if not replaced and os.name == "nt":
        note = (
            "SDK 的 file_editor 描述已变更，未能就地替换 Windows 路径说明，已改为追加"
        )
    guarded = guarded.model_copy(update={"description": description})

    # Write the guarded tool back into the agent's live tool map — the same store
    # ``_execute_action_event`` reads at call time. ``_tools`` is a frozen-model
    # PrivateAttr, so it is replaced via ``object.__setattr__`` exactly as
    # ``AgentBase.add_runtime_tools`` does.
    current = getattr(agent, "_tools", None)
    if not isinstance(current, dict):
        return "agent 工具表不可写（_tools 不是字典）"
    lock = getattr(agent, "_tools_lock", None)
    update = {**current, "file_editor": guarded}
    if lock is not None:
        with lock:
            object.__setattr__(agent, "_tools", update)
    else:
        object.__setattr__(agent, "_tools", update)
    return note


#: The SDK's own tool description tells the model "Always use absolute file paths
#: (starting with /)" (``openhands/tools/file_editor/definition.py``). On Windows
#: that sentence is wrong and — combined with the injected "Your current working
#: directory is: D:/..." — is what makes the model emit ``/D:/...``. The sentence
#: is replaced **in the SDK's own text** (not a copied paraphrase) so the wording
#: still tracks the SDK.
_SDK_BAD_PATH_LINE = "   - Always use absolute file paths (starting with /)"
_HOST_PATH_LINE = (
    "   - Always use absolute file paths. On this Windows host an absolute path "
    "looks like `D:/project/file.py` (or `D:\\project\\file.py`): a drive letter "
    "followed by a colon. Never put a slash in front of a Windows drive path - "
    "`/D:/project/file.py` is invalid and will be rejected."
)


def _host_path_line_fix(text: str) -> tuple[str, bool]:
    """Return ``(description, replaced_in_place)`` with the host-correct wording.

    Applied to the SDK's *live* description (which already carries the injected
    working-directory block), so the wording keeps tracking the SDK instead of
    freezing a paraphrase of it.
    """
    body = text or ""
    if os.name != "nt":
        # On POSIX the SDK's original sentence is correct; leave it untouched.
        return body, True
    if _SDK_BAD_PATH_LINE in body:
        return body.replace(_SDK_BAD_PATH_LINE, _HOST_PATH_LINE), True
    # The SDK moved or reworded the sentence: append rather than silently skip.
    return f"{body}\n{_HOST_PATH_LINE}", False


def _explicit_tools(workspace_path: str = "", emitter: "Emitter | None" = None) -> list:
    """The code agent's tool set **exactly**: ``terminal`` + ``file_editor``.

    INC25 fix — deliberately NOT the SDK default preset. ``get_default_tools()``
    also enables ``task_tracker``, which empirically makes the local qwen model
    wander off-task ("查 pytest help" / "核对项目结构") instead of editing. The
    tool names are read from the live tool classes so the contract tracks the SDK.
    Returns ``[]`` if the imports fail (a tools-less agent is still runnable) and
    **never** falls back to ``get_default_tools()``.

    INC25 P0-C — the terminal tool is constructed **with** a larger no-output
    ("soft") timeout (``no_change_timeout_seconds``). The SDK turns a tool's
    ``params`` into ``TerminalTool.create(conv_state=..., **params)``, so this is
    the supported knob; if the SDK rejects the params shape we fall back to the
    bare names (a working agent is never sacrificed for a nicer timeout).

    INC28 W1 — the action-policy guard is **no longer** installed here. It is
    seated after the conversation has resolved its tools
    (:func:`_install_file_editor_guard`), because this function returns
    ``Tool(name=...)`` *specs* — the real ``ToolDefinition`` (and its executor)
    only exists later. ``workspace_path`` / ``emitter`` are kept in the signature
    so existing callers/tests are unaffected.
    """
    try:
        from openhands.tools.file_editor import FileEditorTool
        from openhands.tools.terminal import TerminalTool
    except Exception:  # noqa: BLE001 — a tools-less agent is still runnable
        return []
    tool_cls = None
    for module_name in ("openhands.sdk", "openhands.tools"):
        try:
            tool_cls = getattr(__import__(module_name, fromlist=["Tool"]), "Tool")
            break
        except Exception:  # noqa: BLE001 — try the next known location
            continue
    if tool_cls is None:
        return []
    try:
        terminal = tool_cls(
            name=TerminalTool.name,
            params={"no_change_timeout_seconds": _TERMINAL_NO_CHANGE_TIMEOUT_S},
        )
    except Exception:  # noqa: BLE001 — older SDK: fall back to the bare spec
        terminal = tool_cls(name=TerminalTool.name)

    return [terminal, tool_cls(name=FileEditorTool.name)]


def _llm_kwargs(job: dict, model: str, base_url: str, api_key: str) -> dict:
    """The ``LLM(...)`` constructor kwargs for the code agent (INC25 fix).

    ``reasoning_effort="none"`` is **load-bearing**: LiteLLM's ``ollama_chat``
    provider translates the SDK default ``"high"`` into Ollama ``think=True``,
    which makes the first round return an empty response (no content, no
    tool_call) and the agent never acts. ``num_ctx`` travels via
    ``litellm_extra_body`` so a long trace is not silently truncated.

    ``temperature`` is the P0-C knob: the local ``qwen3:8b`` is unstable at its
    native default temperature (empirically 2/3 of runs), whose failure shape is a
    terminal soft-timeout retry loop that never finishes (~201s / 15 retries). A
    missing value defaults to **0.0** (deterministic) — never to the model's own
    default. Kept as a seam so a test can prove all three values reach the
    constructor.
    """
    raw_temp = job.get("temperature")
    temperature = float(raw_temp) if raw_temp is not None else 0.0
    kwargs: dict = {
        "model": model,
        "base_url": base_url,
        "reasoning_effort": str(job.get("reasoning_effort") or "none"),
        "temperature": temperature,
        "litellm_extra_body": {"num_ctx": int(job.get("num_ctx") or 32768)},
    }
    if api_key:
        kwargs["api_key"] = api_key
    return kwargs


#: The SDK prepends this header to the observation text when a tool failed
#: (``openhands/sdk/tool/schema.py``::``Observation.ERROR_MESSAGE_HEADER``).
_SDK_ERROR_HEADER = "[An error occurred during execution.]"

#: The visible (business) label for a failed step, per event kind. The raw tool
#: text never becomes the visible label (INC25 AC-13).
_ERROR_LABELS: dict[str, str] = {
    "observation": "步骤产出异常",
    "action": "步骤执行失败",
}


def _observation_text(event: object) -> str:
    """Best-effort plain text of an event's observation / message (``""``)."""
    obs = getattr(event, "observation", None)
    if obs is not None:
        for attr in ("text", "content", "message"):
            value = getattr(obs, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        content = getattr(obs, "content", None)
        if isinstance(content, (list, tuple)):
            parts = [
                str(getattr(c, "text", c))
                for c in content
                if str(getattr(c, "text", c) or "").strip()
            ]
            if parts:
                return "\n".join(parts)
    return ""


def _event_is_error(event: object, text: str) -> bool:
    """Whether an event reports a **failed** tool observation (INC28 W3 + W8).

    Four independent signals, any of which suffices:

    1. the SDK's structured ``Observation.is_error`` flag;
    2. the runner's own "❌" rejection marker;
    3. the SDK's ``ERROR_MESSAGE_HEADER`` prefix on the observation text;
    4. **(INC28 W8)** the SDK's **structured terminal exit status** — a
       ``TerminalObservation`` carries ``exit_code: int | None`` and
       ``timeout: bool`` (``openhands/tools/terminal/definition.py``). This is the
       signal that catches a shell command that *ran but failed* (e.g. PowerShell's
       ``CommandNotFoundException``: ``pytest`` is not a command, exit code 1) yet
       is **not** marked ``is_error`` by the SDK, so the old three-signal version
       left it labelled 「步骤已完成」 — the platform's own honesty claim,
       contradicted by its own timeline.

    Why the exit status is read **structurally** (and never from the text): a
    failed test run prints the words ``error`` / ``failed`` in its *output*, so a
    text heuristic would paint a **successful** test step red. The exit code is
    the mechanism's own verdict, so it is trusted; the visible label stays
    business copy and the raw output only ever reaches ``detail``.

    Conservative by construction — the two "not a failure" cases are left as-is:

    * ``exit_code is None`` ⇒ **not** judged (this SDK allows ``None`` =
      *unmeasured*; a missing measurement is not evidence of failure);
    * ``exit_code == -1`` ⇒ **not** judged (the SDK's *soft-timeout / not yet
      finished* sentinel — "still running" is not "failed", and a hard timeout is
      already reported by the run's own ``degraded="timeout"``, so re-judging it
      here would only manufacture a second, spurious red).

    ``timeout is True`` ⇒ judged (an explicit timeout flag **is** a failure).
    """
    obs = getattr(event, "observation", None)
    if obs is not None and bool(getattr(obs, "is_error", False)):
        return True
    probe = (text or "").strip()
    if "❌" in probe:
        return True
    if probe.startswith(_SDK_ERROR_HEADER):
        return True
    # INC28 W8 — 4th, structured signal: the terminal process's own exit status.
    if obs is not None:
        if bool(getattr(obs, "timeout", False)):
            return True
        exit_code = getattr(obs, "exit_code", None)
        if exit_code is not None and exit_code not in (0, -1):
            return True
    return False


def _emit_oh_event(emitter: Emitter, event: object) -> None:
    """Adapt one OpenHands event onto a business-labelled runner event.

    INC28 W3 — an **error observation** must not be flattened into ``ok`` /
    「步骤已完成」. The SDK marks a failed tool observation with
    ``Observation.is_error`` and prepends
    ``Observation.ERROR_MESSAGE_HEADER`` ("[An error occurred ...]") to its text;
    the runner's own rejection observation carries a "❌" marker. Any of those
    signals maps this event to ``status="error"`` with the **existing** error
    copy as the visible label. The raw tool / observation text still goes only
    into ``detail`` (never into the visible ``label``), and ``latency_ms`` stays
    unset (``None``) because the runner never measured it.

    INC28 W9 — ``AgentErrorEvent`` is registered too. It is an
    ``ObservationBaseEvent`` (``openhands/sdk/event/llm_convertible/observation.py``)
    carrying a required ``error: str`` and a ``classification``; it describes an
    error **produced by the agent/scaffold**, i.e. the same *class of event* as
    ``ConversationErrorEvent``. It is therefore mapped to the **same**
    ``phase="error"`` / ``kind="engine"`` / ``status="error"`` / business label,
    never the unregistered ``ok`` default — a real error must not read as success.

    The ``classification`` field distinguishes *where* an error surfaced, not
    *why*: ``classification.kind == AGENT_ACTION`` marks an **expected,
    agent-self-correctable validation failure** (the producer opts in explicitly);
    anything else (including the default ``UNKNOWN``) is an unexpected
    diagnostic. That distinction is deliberately **not** graded here — either way
    the step did **not succeed**, so both map to ``status="error"``. It is recorded
    as the future basis for a mild/severe split, not implemented this round.

    No double reporting: the SDK emits **one** observation-plane event per action —
    an ``AgentErrorEvent`` *instead of* an ``ObservationEvent`` when the action
    execution raised, or an ``ObservationEvent`` when the tool ran (and may itself
    carry a non-zero ``exit_code``, W8). The two are distinct events for distinct
    actions, so a single failure is never counted twice.
    """
    name = type(event).__name__
    mapping = {
        "ConversationStateUpdateEvent": ("action", "state", "running", "正在执行"),
        "ActionEvent": ("action", "action", "running", "正在执行"),
        "ObservationEvent": ("observation", "observation", "ok", "步骤已完成"),
        "MessageEvent": ("plan", "message", "ok", "生成修改方案"),
        "HookExecutionEvent": ("observation", "hook", "ok", "引擎钩子已执行"),
        # The SDK's own terminal error (e.g. ``MaxIterationsReached``). Without this
        # it would fall through to the "unregistered event / ok" default and the one
        # event that explains the truncation would read as a success.
        "ConversationErrorEvent": ("error", "engine", "error", "引擎报告执行错误"),
        # INC28 W9 — ``AgentErrorEvent`` (``openhands/sdk/event/llm_convertible/
        # observation.py``) is an ``ObservationBaseEvent`` describing an error the
        # agent/scaffold produced (a required ``error: str`` + a ``classification``).
        # It is the **same class of event** as ``ConversationErrorEvent`` above
        # (an engine-originated error), so it is mapped **identically**
        # (``phase="error"`` / ``kind="engine"`` / ``status="error"`` / the same
        # business label) instead of falling through to the unregistered default
        # that would render a real error as ``ok``/「（未登记的引擎事件）」.
        "AgentErrorEvent": ("error", "engine", "error", "引擎报告执行错误"),
    }
    phase, kind, status, label = mapping.get(
        name, ("observation", "other", "ok", "（未登记的引擎事件）")
    )
    detail = ""
    # INC28 W9 — ``error`` is read FIRST: it is the only structured message among
    # these that carries an agent-scaffold error verbatim (``AgentErrorEvent.error``),
    # and it must win over that same event's ``tool_name`` (which would otherwise be
    # picked first and hide the error text). No other mapped event has a string
    # ``error`` attribute, so putting it first is a no-op for them. The raw text
    # only ever lands in ``detail`` — never in the visible ``label``.
    for attr in ("error", "tool_name", "action", "observation", "message", "content", "thought"):
        value = getattr(event, attr, None)
        if isinstance(value, str) and value.strip():
            detail = value.strip()
            break
    tool = getattr(event, "tool_name", None) or getattr(event, "tool", None)

    # INC28 W3 — surface a failed observation as an error, never as "done".
    obs_text = _observation_text(event)
    if detail and obs_text and obs_text not in detail:
        detail = f"{detail}\n{obs_text}"
    elif not detail and obs_text:
        detail = obs_text
    if status == "ok" and _event_is_error(event, obs_text or detail):
        status = "error"
        label = _ERROR_LABELS.get(kind, "步骤产出异常")

    emitter.emit(phase, kind, status, label, tool=str(tool) if tool else None, detail=detail)


class _EventRelay:
    """Adapt OpenHands events to runner events, each exactly once.

    Events can reach the runner through **two** channels — the ``callbacks=``
    hook invoked during ``Conversation.run()`` and the final
    ``conversation.state.events`` ledger. The same event may therefore be seen
    twice; the relay de-duplicates by a stable fingerprint so the timeline never
    shows one step twice, and counts what it really emitted.

    INC25 P0-C — the relay also owns the **round budget** observation: it counts
    ``ActionEvent``s (one per agent action) and flips :attr:`limit_hit` when the
    budget is spent. The *actual* stop is the SDK's native
    ``max_iteration_per_run`` (see :func:`_run_conversation`); the relay is the
    honest detector that lets the runner report the truncation verbatim as
    ``degraded="max_rounds"`` instead of a silent success.
    """

    def __init__(self, emitter: "Emitter", max_rounds: int = 0) -> None:
        self._emitter = emitter
        self._seen: set[str] = set()
        self.count = 0
        self.max_rounds = int(max_rounds or 0)
        self.action_count = 0
        self.limit_hit = False
        self._stopped = False

    @staticmethod
    def _fingerprint(event: object) -> str:
        raw_id = getattr(event, "id", None)
        if isinstance(raw_id, str) and raw_id:
            return f"id:{raw_id}"
        return f"obj:{id(event)}"

    def stop(self) -> None:
        """Gate further emission.

        Called when the wall clock abandons a stuck run: the abandoned daemon
        thread must not keep writing events into the JSONL *after* the runner has
        moved on to its aborted tail, or the two writers would interleave.
        """
        self._stopped = True

    def handle(self, event: object) -> None:
        """Callback entry point: adapt ``event`` once (no-op on a repeat)."""
        if event is None or self._stopped:
            return
        fingerprint = self._fingerprint(event)
        if fingerprint in self._seen:
            return
        self._seen.add(fingerprint)
        _emit_oh_event(self._emitter, event)
        self.count += 1

        name = type(event).__name__
        if name == "ActionEvent":
            self.action_count += 1
            if self.max_rounds and self.action_count >= self.max_rounds:
                self.limit_hit = True
        # The SDK's own native signal that ``max_iteration_per_run`` was reached
        # (``ConversationErrorEvent(code="MaxIterationsReached")``). Authoritative
        # and independent of our own counting.
        if str(getattr(event, "code", "")) == "MaxIterationsReached":
            self.limit_hit = True

    def drain(self, events: object) -> int:
        """Feed an iterable of events; returns how many were newly emitted."""
        if events is None:
            return 0
        try:
            iterator = iter(events)  # type: ignore[arg-type]
        except TypeError:
            return 0
        added = 0
        for event in iterator:
            before = self.count
            self.handle(event)
            if self.count > before:
                added += 1
        return added


def _event_callbacks(relay: _EventRelay) -> list:
    """The OpenHands ``callbacks=`` argument.

    Kept as a tiny seam so a test can prove the callback wiring is load-bearing:
    with it the engine events arrive; with it removed they do not.
    """
    return [relay.handle]


def _is_event_iterable(value: object) -> bool:
    """True when ``value`` is a real iterable of events (not ``None``/str/dict)."""
    return (
        value is not None
        and not isinstance(value, (str, bytes, dict))
        and hasattr(value, "__iter__")
    )


def _build_conversation(
    conversation_cls, *, agent, workspace_obj, callbacks, max_iteration_per_run=None
):
    """Construct the conversation, degrading (never raising) on an SDK shape gap.

    Returns ``(conversation, callback_error)``. ``callbacks`` is passed when the
    SDK accepts it (the primary event channel); if the kwarg is rejected the
    caller still has the ``state.events`` ledger as a fallback and reports a
    verbatim ``callback_error`` so a missing-event case is never silent.

    INC25 P0-C — ``max_iteration_per_run`` is the SDK's **native** per-run round
    cap (``openhands.sdk.conversation.conversation.Conversation.__new__`` accepts
    it, default 500; verified against the installed SDK). Passing it makes the
    SDK do the real stop; our relay only *observes* it. A value of 0/None means
    "no cap from us" (the SDK default applies).
    """
    extra: dict = {}
    if max_iteration_per_run and int(max_iteration_per_run) > 0:
        extra["max_iteration_per_run"] = int(max_iteration_per_run)

    with_ws: list[dict] = []
    if workspace_obj is not None:
        with_ws = [{"agent": agent, "workspace": workspace_obj, "callbacks": callbacks, **extra}]
    attempts: list[dict] = with_ws + [{"agent": agent, "callbacks": callbacks, **extra}]

    callback_error = ""
    for kwargs in attempts:
        try:
            return conversation_cls(**kwargs), ""
        except TypeError as exc:  # likely an unsupported kwarg — try less
            callback_error = repr(exc)

    # Older SDK without ``callbacks``: fall back and rely on the state ledger.
    fallbacks = ([{"agent": agent, "workspace": workspace_obj, **extra}] if workspace_obj is not None
                 else []) + [{"agent": agent, **extra}]
    for kwargs in fallbacks:
        try:
            return conversation_cls(**kwargs), callback_error
        except TypeError:
            continue
    raise TypeError("Conversation(...) 不被当前 SDK 接受")


def _run_with_wall_clock(conversation, wall_timeout_s: int):
    """Drive ``conversation.run()`` under a wall-clock ceiling (INC25 P0-C).

    ``run()`` is a blocking call with no native deadline, so it is executed on a
    **daemon** thread and abandoned once ``wall_timeout_s`` elapses. On expiry we
    raise :class:`_WallClockExceeded` from the *main* thread; the still-running
    daemon thread is reaped when the process exits. A non-positive timeout means
    "no internal ceiling" (the engine's whole-process cap still applies).
    """
    if not wall_timeout_s or wall_timeout_s <= 0:
        return conversation.run()

    holder: dict = {}

    def _target() -> None:
        try:
            holder["result"] = conversation.run()
        except BaseException as exc:  # noqa: BLE001 — surfaced on the main thread
            holder["exc"] = exc

    thread = threading.Thread(target=_target, name="openhands-conversation", daemon=True)
    thread.start()
    thread.join(timeout=wall_timeout_s)
    if thread.is_alive():
        raise _WallClockExceeded(wall_timeout_s)
    if "exc" in holder:
        raise holder["exc"]
    return holder.get("result")


def _run_conversation(emitter: Emitter, job: dict, workspace_path: str) -> None:
    """Build and drive the OpenHands conversation. Raises on any SDK failure.

    OpenHands' ``Conversation.run()`` returns ``None`` (it is not an event
    iterable), so events are captured through the ``callbacks=`` hook and then
    reconciled against ``conversation.state.events`` — the same event seen twice
    is emitted once (see :class:`_EventRelay`).

    INC25 P0-C — two hard ceilings are enforced here (see ``_drive`` for the
    verbatim reporting, which never lets a truncated run look like a success):

    * **rounds** — handed to the SDK as its native ``max_iteration_per_run``; the
      relay observes the SDK's own ``MaxIterationsReached`` signal (and counts
      actions) and this function raises :class:`_MaxRoundsExceeded` afterwards.
    * **wall clock** — ``run()`` runs on a daemon thread and is abandoned after
      ``job["wall_timeout_s"]`` (:func:`_run_with_wall_clock`), raising
      :class:`_WallClockExceeded`.
    """
    from openhands.sdk import LLM, Agent, Conversation

    model = str(job.get("model") or "ollama_chat/qwen3:8b")
    base_url = str(job.get("base_url") or "http://127.0.0.1:11434")
    api_key = str(job.get("api_key") or "")
    intent = str(job.get("task_intent") or "").strip()
    max_rounds = int(job.get("max_rounds") or 0)
    wall_timeout_s = int(job.get("wall_timeout_s") or 0)
    # Cap the *conversation* a slice below the engine's whole-process wall clock, so
    # the runner still gets to emit the diff and the terminal ``result`` line before
    # the engine's ``asyncio.wait_for`` fires (see ``_WRAP_UP_RESERVE_SECONDS``).
    conversation_budget_s = (
        max(1, wall_timeout_s - _WRAP_UP_RESERVE_SECONDS) if wall_timeout_s > 0 else 0
    )

    llm = LLM(**_llm_kwargs(job, model, base_url, api_key))

    # INC27 — the workspace path is what the policy rules are anchored to, so the
    # guard can only be installed once the workspace is known.
    tools = _explicit_tools(workspace_path, emitter)
    # INC25 fix — the custom system prompt is what keeps the local model acting
    # instead of Q&A-ing about the project. If the SDK rejects the kwarg, degrade
    # to the tools-only constructor but say so verbatim (never a silent fallback).
    # INC27 - the platform's selected context is APPENDED, never substituted: the
    # minimal prompt above is what keeps the local qwen model acting.
    system_prompt = _SYSTEM_PROMPT + _context_block(job)
    try:
        agent = Agent(llm=llm, tools=tools, system_prompt=system_prompt)
    except TypeError:
        agent = Agent(llm=llm, tools=tools)
        system_prompt = ""
    if not system_prompt:
        emitter.emit(
            "plan", "message", "error", "自定义系统提示未被 SDK 接受（回退默认提示词）",
            detail="Agent(system_prompt=...) 被拒绝",
        )

    emitter.emit("plan", "message", "ok", "生成修改方案", detail=intent)

    workspace_obj = _make_workspace(workspace_path)

    relay = _EventRelay(emitter, max_rounds=max_rounds)
    conversation, callback_error = _build_conversation(
        Conversation,
        agent=agent,
        workspace_obj=workspace_obj,
        callbacks=_event_callbacks(relay),
        max_iteration_per_run=max_rounds,
    )

    # INC28 W1 — seat the action-policy guard on the *resolved* file_editor tool,
    # now that the conversation has materialised its tool map. Losing the guard
    # must never look like having it: a non-empty note is reported verbatim.
    guard_note = _install_file_editor_guard(conversation, workspace_path, emitter)
    if guard_note:
        emitter.emit(
            "engine_ready", "engine", "error", "代码动作策略未启用（已降级）",
            detail=guard_note,
        )
    else:
        emitter.emit(
            "engine_ready", "engine", "ok", "代码动作策略已启用",
            detail=f"rules={[r.name for r in _build_policy_rules(workspace_path)]}",
        )

    conversation.send_message(intent)
    try:
        result = _run_with_wall_clock(conversation, conversation_budget_s)
    except _WallClockExceeded:
        # The conversation thread is abandoned (still running) — silence it so it
        # cannot interleave its events with the aborted tail ``_drive`` emits next.
        relay.stop()
        raise

    # Channel 1: some SDK shapes return this run's events as an iterable.
    if _is_event_iterable(result):
        relay.drain(result)

    # Channel 2: the authoritative per-run ledger — covers the real SDK, where
    # run() returns None and events arrive via callbacks and/or the state ledger.
    state = getattr(conversation, "state", None)
    relay.drain(getattr(state, "events", None))

    # Callbacks rejected AND nothing observed ⇒ say so verbatim, never a silent
    # timeline with no engine steps.
    if callback_error and relay.count == 0:
        reason = f"未能从引擎获取事件：{callback_error}"
        emitter.emit(
            "observation", "other", "unavailable", "引擎事件未获取",
            detail=reason, data={"degraded": "engine_unavailable", "reason": reason},
        )

    # The round budget was spent without finishing ⇒ a truncation. The caller
    # reports it verbatim as ``degraded="max_rounds"`` (never a silent success).
    if relay.limit_hit and max_rounds:
        raise _MaxRoundsExceeded(max_rounds, relay.action_count)


def _emit_diff(emitter: Emitter, workspace_path: str) -> None:
    """Emit the real workspace diff (a no-change run is ``not_applicable``)."""
    env = dict(os.environ)
    diff_text = ""
    try:
        subprocess.run(
            ["git", "-C", workspace_path, "add", "-A"],
            env=env, capture_output=True, timeout=30, check=False,
        )
        completed = subprocess.run(
            ["git", "-C", workspace_path, "diff", "--cached", "--no-color"],
            env=env, capture_output=True, text=True, timeout=30, check=False,
        )
        diff_text = completed.stdout or ""
    except Exception as exc:  # noqa: BLE001 — a missing git is a note, not a crash
        emitter.emit("diff", "diff", "error", "生成代码变更失败", detail=str(exc))
        diff_text = ""

    if diff_text.strip():
        emitter.emit("diff", "diff", "ok", "已生成代码变更", data={"diff": diff_text})
    else:
        emitter.emit("diff", "diff", "not_applicable", "本次无代码变更")


def _emit_tests(
    emitter: Emitter, workspace_path: str, test_command: str, wall_timeout_s: int
) -> int | None:
    """Run the in-workspace test command and emit its raw output. Returns exit code."""
    env = dict(os.environ)
    command = (test_command or "").strip()
    if not command:
        emitter.emit("test", "test", "unavailable", "测试未执行", detail="未配置测试命令")
        return None

    start = time.monotonic()
    try:
        completed = subprocess.run(
            command, cwd=workspace_path, env=env, shell=True,
            capture_output=True, text=True,
            timeout=max(30, int(wall_timeout_s or _TEST_TIMEOUT_SECONDS)),
        )
        raw_stdout = (completed.stdout or "")
        if completed.stderr:
            raw_stdout = f"{raw_stdout}\n{completed.stderr}"
        exit_code = completed.returncode
    except subprocess.TimeoutExpired:
        emitter.emit("test", "test", "error", "测试执行超时", detail=command)
        return None
    except Exception as exc:  # noqa: BLE001 — a test harness failure is reported verbatim
        emitter.emit("test", "test", "error", "测试执行失败", detail=str(exc))
        return None

    latency_ms = (time.monotonic() - start) * 1000.0
    emitter.emit(
        "test", "test", "ok" if exit_code == 0 else "error",
        "测试已执行" if exit_code == 0 else "测试未通过",
        detail=command,
        data={"raw_stdout": raw_stdout, "exit_code": exit_code},
        latency_ms=latency_ms,
    )
    return exit_code


def _emit_diff_and_tests(
    emitter: Emitter, workspace_path: str, test_command: str, wall_timeout_s: int
) -> int | None:
    """Emit the real workspace diff and the raw test output. Returns test exit code."""
    _emit_diff(emitter, workspace_path)
    return _emit_tests(emitter, workspace_path, test_command, wall_timeout_s)


def _emit_aborted_tail(emitter: Emitter, workspace_path: str, reason: str) -> None:
    """Emit the diff for a run that was **aborted** (rounds/wall clock).

    The test command is deliberately skipped: the run did not finish, so running
    the suite only burns the wall-clock budget we just exhausted. The skip is
    reported verbatim (``not_applicable``) — never a fake "tests passed".
    """
    _emit_diff(emitter, workspace_path)
    emitter.emit("test", "test", "not_applicable", "测试未执行（本次运行已中止）", detail=reason)


def main() -> int:
    job = _read_job()
    # The JSONL channel is the process's REAL stdout. The OpenHands SDK also prints
    # a rich human console to stdout; interleaved with the JSONL the engine reads
    # every such line as unparseable and spuriously degrades the whole run to
    # ``parse_failed``. So the emitter keeps a private handle on the real stdout and
    # the process ``sys.stdout`` is redirected to stderr for the duration.
    real_stdout = sys.stdout
    emitter = Emitter(real_stdout)
    sys.stdout = sys.stderr
    try:
        return _drive(job, emitter)
    finally:
        sys.stdout = real_stdout


def _drive(job: dict, emitter: Emitter) -> int:
    workspace_path = str(job.get("workspace_path") or os.getcwd())
    model = str(job.get("model") or "ollama_chat/qwen3:8b")
    base_url = str(job.get("base_url") or "http://127.0.0.1:11434")
    test_command = str(job.get("test_command") or "")
    wall_timeout_s = int(job.get("wall_timeout_s") or 0) or _TEST_TIMEOUT_SECONDS

    if not os.path.isdir(workspace_path):
        reason = f"工作区不存在：{workspace_path}"
        emitter.emit(
            "workspace", "engine", "unavailable", "隔离工作区不可用",
            detail=reason, data={"degraded": "engine_unavailable", "reason": reason},
        )
        emitter.emit("done", "result", "error", "执行异常退出", data={"exit_code": 1})
        return 1

    emitter.emit("workspace", "workspace", "ok", "隔离工作区已就绪", detail=workspace_path)

    try:
        import openhands.sdk  # noqa: F401 — availability probe
    except Exception as exc:  # noqa: BLE001 — the OpenHands SDK is an optional host dep
        reason = f"无法导入 openhands SDK：{exc!r}"
        emitter.emit(
            "engine_ready", "engine", "unavailable", "代码执行引擎不可用",
            detail=reason, data={"degraded": "engine_unavailable", "reason": reason},
        )
        emitter.emit("done", "result", "error", "执行异常退出", data={"exit_code": 1})
        return 1

    emitter.emit(
        "engine_ready", "engine", "ok", "代码执行引擎已就绪",
        data={"model": model, "base_url": base_url},
    )

    try:
        _run_conversation(emitter, job, workspace_path)
    except _MaxRoundsExceeded as exc:
        # Truncation by the round budget — an *error*, reported verbatim so it can
        # never be mistaken for a completed run (engine maps it to
        # ``codeplane.degraded == "max_rounds"``).
        reason = str(exc)
        emitter.emit(
            "action", "engine", "error", "已达最大回合上限，已中止",
            detail=reason, data={"degraded": "max_rounds", "reason": reason},
        )
        _emit_aborted_tail(emitter, workspace_path, reason)
        emitter.emit("done", "result", "error", "执行异常退出", data={"exit_code": 1})
        return 1
    except _WallClockExceeded as exc:
        # Truncation by the wall clock — an *error*, reported verbatim.
        reason = str(exc)
        emitter.emit(
            "action", "engine", "error", "代码任务执行超时已中止",
            detail=reason, data={"degraded": "timeout", "reason": reason},
        )
        _emit_aborted_tail(emitter, workspace_path, reason)
        emitter.emit("done", "result", "error", "执行异常退出", data={"exit_code": 1})
        return 1
    except Exception as exc:  # noqa: BLE001 — every failure degrades explicitly
        degraded, reason = _classify_conversation_error(exc)
        emitter.emit(
            "engine_ready", "engine", "unavailable",
            "本机模型不可用" if degraded == "model_unavailable" else "代码执行引擎异常",
            detail=reason, data={"degraded": degraded, "reason": reason},
        )
        _emit_diff_and_tests(emitter, workspace_path, test_command, wall_timeout_s)
        emitter.emit("done", "result", "error", "执行异常退出", data={"exit_code": 1})
        return 1

    _emit_diff_and_tests(emitter, workspace_path, test_command, wall_timeout_s)
    emitter.emit("done", "result", "ok", "执行完成", data={"exit_code": 0})
    return 0


if __name__ == "__main__":
    sys.exit(main())
