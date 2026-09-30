"""The code execution engine (INC25 W2, design §2.1 / §7).

``SubprocessOpenHandsEngine`` runs a code task as a **managed subprocess**: it
launches ``codeplane/runner/run_code_task.py`` with the *OpenHands* interpreter,
writes a JSON job to its stdin, reads JSONL events from its stdout, and parses
them into a :class:`CodeRunResult`.

The subprocess boundary is what buys the three properties this increment needs:
**startup-time availability check** (can the process even start?), **tree-level
cancellation** (terminate the child), and a **wire contract we own**.

Proxy hygiene (highest-risk detail, design §9): the sandbox carries
``HTTP_PROXY`` / ``HTTPS_PROXY``. A code agent talking to local Ollama through a
proxy gets a 5xx and silently degrades into deterministic orchestration. So
:meth:`SubprocessOpenHandsEngine._env` **strips every ``*_PROXY`` variable (both
cases)** and sets ``NO_PROXY=127.0.0.1,localhost,::1`` on the child environment.

Degradation is always explicit (never a 5xx, never a fake success): an engine that
cannot start, a timeout, a non-zero exit without a result line, garbled JSONL, or
an engine-reported model outage each map to a ``codeplane.degraded`` value.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from forgeflow.codeplane.events import CodeEvent, adapt_openhands_event, summarize_timeline
from forgeflow.codeplane.protocol import (
    DEGRADED_VALUES,
    RESULT_KIND,
    decode_line,
)
from forgeflow.codeplane.tests_verdict import TestResult, evaluate_test_output
from forgeflow.config import get_settings
from forgeflow.repositories.base import utcnow

logger = logging.getLogger(__name__)

__all__ = [
    "CodeJob",
    "CodeRunResult",
    "CodePlaneEngine",
    "SubprocessOpenHandsEngine",
    "AgentServerOpenHandsEngine",
    "get_code_engine",
    "reset_code_engine",
]

#: The bundled runner script (never imported by ForgeFlow — spawned only).
_DEFAULT_RUNNER = Path(__file__).resolve().parent / "runner" / "run_code_task.py"

#: The thin agent server entry point (INC29 §4) — spawned only, never imported.
_DEFAULT_SERVER_ENTRY = (
    Path(__file__).resolve().parent / "runner" / "agent_server" / "__main__.py"
)

#: The per-process session key the launched server reads (never via argv — argv is
#: visible in the process table).
_SESSION_API_KEY_ENV = "SESSION_API_KEY"

#: The server liveness probe the client polls until it answers 200 (INC29 §4.1).
_HEALTH_PATH = "/api/conversations/count"

#: Proxy variables stripped from the child environment (both cases — design §9).
_PROXY_VARS: tuple[str, ...] = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
)

_NO_PROXY = "127.0.0.1,localhost,::1"

#: Degraded values that describe "the engine/model never produced a run" (vs an
#: execution *error*). These render as ``unavailable``; the rest render as ``error``.
_UNAVAILABLE_DEGRADES: frozenset[str] = frozenset({"engine_unavailable", "model_unavailable"})


@dataclass
class CodeJob:
    """The job object written to the runner's stdin."""

    run_id: str = ""
    task_intent: str = ""
    workspace_path: str = ""
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    max_rounds: int = 0
    wall_timeout_s: int = 0
    test_command: str = ""
    language_hint: str = ""
    # INC25 fix — LLM knobs the runner would otherwise have to guess. The engine
    # is the single source of truth (Settings), so both travel inside the job.
    reasoning_effort: str = ""
    num_ctx: int = 0
    # INC25 P0-C — ``0.0`` is a *valid* temperature, so this stays ``None`` until
    # the engine fills it from Settings; the runner then defaults a missing value
    # to 0.0 (never to the model's unstable native default).
    temperature: float | None = None
    # INC27 — the enterprise context ForgeFlow (the control plane) selected for
    # this run, handed to OpenHands (the execution plane). Both default to ``[]``
    # so every existing construction site stays valid and a run with no context is
    # honestly "nothing was injected" rather than a fabricated block.
    skill_context: list[dict[str, Any]] = field(default_factory=list)
    memory_context: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_intent": self.task_intent,
            "workspace_path": self.workspace_path,
            "model": self.model,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "max_rounds": self.max_rounds,
            "wall_timeout_s": self.wall_timeout_s,
            "test_command": self.test_command,
            "language_hint": self.language_hint,
            "reasoning_effort": self.reasoning_effort,
            "num_ctx": self.num_ctx,
            "temperature": self.temperature,
            "skill_context": list(self.skill_context),
            "memory_context": list(self.memory_context),
        }


@dataclass
class CodeRunResult:
    """The engine's outcome for one code task."""

    status: str = "ok"                 # ok | error | unavailable
    timeline: list[CodeEvent] = field(default_factory=list)
    diff: str = ""
    tests: TestResult = field(default_factory=TestResult)
    exit_code: int = -1
    degraded: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    #: INC29 T02 (§6) — the progress-summary vocabulary
    #: (``files_changed`` / ``test_command`` / ``passed`` / ``failed`` /
    #: ``repair_rounds``), every value taken from real evidence. Empty until
    #: ``_parse`` fills it (a degraded run that never ran a task keeps ``{}``).
    summary: dict[str, Any] = field(default_factory=dict)
    #: INC29 T04 (§5.1) — the transport that produced this result
    #: (``"subprocess"`` | ``"agent_server"``). Additive with a safe default so
    #: every existing construction site keeps working.
    transport: str = "subprocess"
    #: INC29 T04 (§5.3) — when ``transport="auto"`` fell back, the transport it
    #: fell back *from* (``"agent_server"``); ``None`` on the normal path. Recorded
    #: honestly rather than silently.
    fell_back_from: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "timeline": [e.to_dict() for e in self.timeline],
            "diff": self.diff,
            "tests": self.tests.to_dict(),
            "exit_code": self.exit_code,
            "degraded": self.degraded,
            "raw": dict(self.raw),
            "summary": dict(self.summary),
            "transport": self.transport,
            "fell_back_from": self.fell_back_from,
        }


@runtime_checkable
class CodePlaneEngine(Protocol):
    """The engine surface the orchestrator drives (design §4)."""

    def available(self) -> bool: ...

    async def run(self, job: CodeJob) -> CodeRunResult: ...

    async def cancel(self, handle: Any) -> None: ...


def _interpreter_ok(interp: str) -> bool:
    if not interp:
        return False
    if os.sep in interp or "/" in interp or "\\" in interp:
        return Path(interp).exists()
    return shutil.which(interp) is not None


def _merge_job_defaults(job: CodeJob, settings: Any) -> CodeJob:
    """Fill a job's unset fields from ``Settings`` (shared by both transports).

    Module-level so the subprocess and agent-server engines cannot drift. The
    engine is the single source of truth for these knobs (Settings), so they
    travel inside the job instead of being re-guessed downstream.
    """
    s = settings
    if not job.model:
        job.model = s.codeplane_model_name() if hasattr(s, "codeplane_model_name") else "ollama_chat/qwen3:8b"
    if not job.base_url:
        job.base_url = s.codeplane_base() if hasattr(s, "codeplane_base") else s.ollama_base_url
    if not job.max_rounds:
        job.max_rounds = int(getattr(s, "codeplane_max_rounds", 30))
    if not job.wall_timeout_s:
        job.wall_timeout_s = int(getattr(s, "codeplane_timeout_seconds", 180))
    if not job.test_command:
        job.test_command = str(getattr(s, "codeplane_test_command", "python -m pytest -q"))
    if not job.reasoning_effort:
        job.reasoning_effort = str(getattr(s, "codeplane_reasoning_effort", "none") or "none")
    if not job.num_ctx:
        job.num_ctx = int(getattr(s, "codeplane_num_ctx", 32768) or 32768)
    # ``temperature`` is filled from Settings only when the caller left it
    # unset: 0.0 is a legitimate value, so a truthiness check would be wrong.
    if job.temperature is None:
        job.temperature = float(getattr(s, "codeplane_temperature", 0.0) or 0.0)
    return job


class SubprocessOpenHandsEngine:
    """Managed-subprocess implementation of :class:`CodePlaneEngine`."""

    def __init__(
        self,
        interpreter: str | None = None,
        runner_argv: list[str] | None = None,
        workspace_manager: Any = None,
        settings: Any = None,
    ) -> None:
        self._settings = settings or get_settings()
        explicit = interpreter if interpreter is not None else ""
        self._interpreter = (
            explicit
            or getattr(self._settings, "codeplane_interpreter", "")
            or os.environ.get("FORGEFLOW_CODEPLANE_PYTHON", "")
        ).strip()
        argv = runner_argv if runner_argv else [str(_DEFAULT_RUNNER)]
        self._runner_argv = [str(a) for a in argv]
        self._workspace_manager = workspace_manager
        self._enabled = bool(getattr(self._settings, "codeplane_enabled", True))
        self._reason = ""
        self._proc: Any = None

    # ---------------------------------------------------------------- #
    # Availability                                                     #
    # ---------------------------------------------------------------- #
    def available(self) -> bool:
        """True only when the enabled engine's interpreter + runner both exist.

        Cheap and side-effect-free: it never imports ``openhands`` (that runs in
        the child process). A host without the OpenHands venv correctly reports
        ``False`` here, which becomes a structured degrade — not a crash.
        """
        if not self._enabled:
            self._reason = "代码执行面已关闭（Settings.codeplane_enabled=false）"
            return False
        if not self._interpreter:
            self._reason = (
                "未配置代码执行面解释器"
                "（Settings.codeplane_interpreter / FORGEFLOW_CODEPLANE_PYTHON 均为空）"
            )
            return False
        if not _interpreter_ok(self._interpreter):
            self._reason = f"代码执行面解释器不存在：{self._interpreter}"
            return False
        runner = self._runner_argv[0] if self._runner_argv else ""
        if not runner or not Path(runner).exists():
            self._reason = f"runner 脚本不存在：{runner}"
            return False
        return True

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def interpreter(self) -> str:
        return self._interpreter

    # ---------------------------------------------------------------- #
    # Environment (proxy hygiene — the highest-risk detail)            #
    # ---------------------------------------------------------------- #
    def _env(self) -> dict[str, str]:
        """Child environment with every proxy variable removed (design §9).

        ``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``ALL_PROXY`` (and their lower-case
        twins) are deleted and ``NO_PROXY`` is pinned to loopback, so the child's
        httpx/LiteLLM call to local Ollama cannot be sent through the sandbox
        proxy (which would 502 and silently degrade the run).
        """
        env = dict(os.environ)
        for name in _PROXY_VARS:
            env.pop(name, None)
        env["NO_PROXY"] = _NO_PROXY
        env["no_proxy"] = _NO_PROXY
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    # ---------------------------------------------------------------- #
    # Run                                                              #
    # ---------------------------------------------------------------- #
    def _merge_defaults(self, job: CodeJob) -> CodeJob:
        return _merge_job_defaults(job, self._settings)

    async def run(self, job: CodeJob) -> CodeRunResult:
        if not self.available():
            return self._degraded("engine_unavailable", self._reason)
        job = self._merge_defaults(job)
        argv = [self._interpreter, *self._runner_argv]
        env = self._env()
        payload = json.dumps(job.to_dict(), ensure_ascii=False).encode("utf-8")
        wall = int(job.wall_timeout_s or getattr(self._settings, "codeplane_timeout_seconds", 180))
        cwd = job.workspace_path or None
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
        except (FileNotFoundError, OSError, ValueError) as exc:
            return self._degraded("engine_unavailable", f"无法启动代码执行引擎：{exc}")

        self._proc = proc
        try:
            # ``Popen.communicate`` is blocking, so it runs in a worker thread.
            # This is deliberate: ``asyncio`` can spawn subprocesses only on the
            # Proactor loop on Windows, while the psycopg async checkpointer needs
            # a Selector loop — a thread-based spawn behaves identically on BOTH,
            # so the code plane and the postgres profile can share one loop.
            stdout_b, stderr_b = await asyncio.to_thread(proc.communicate, payload, wall)
        except subprocess.TimeoutExpired:
            await self._kill(proc)
            return self._degraded(
                "timeout", f"代码执行引擎在 {wall}s 内未结束，已中止（timeout）"
            )
        finally:
            self._proc = None

        stdout = (stdout_b or b"").decode("utf-8", errors="replace")
        stderr = (stderr_b or b"").decode("utf-8", errors="replace")
        exit_code = proc.returncode if proc.returncode is not None else -1
        return self._parse(stdout, stderr, exit_code, job)

    async def cancel(self, handle: Any = None) -> None:
        await self._kill(handle if handle is not None else self._proc)

    # ---------------------------------------------------------------- #
    # Parsing / degradation                                            #
    # ---------------------------------------------------------------- #
    def _parse(self, stdout: str, stderr: str, exit_code: int, job: CodeJob) -> CodeRunResult:
        raw_events: list[dict[str, Any]] = []
        unparsed = 0
        for line in (stdout or "").splitlines():
            if not line.strip():
                continue
            parsed = decode_line(line)
            if parsed is None:
                unparsed += 1
            else:
                raw_events.append(parsed)

        timeline = [adapt_openhands_event(evt, seq=i) for i, evt in enumerate(raw_events)]
        result_line = next(
            (r for r in reversed(raw_events) if str(r.get("kind")) == RESULT_KIND), None
        )

        # Engine-reported degradation. The runner marks a giving-up run with an
        # ``engine`` event carrying ``data.degraded`` — that is the canonical
        # signal REGARDLESS of the event's step status (a truncation such as
        # ``max_rounds`` is an *error*, not "unavailable"). We scan every engine
        # event and keep the LAST explicit degrade, so a late, more specific
        # report (e.g. a mid-run model outage that happens *after* the
        # ``engine_ready`` ok notice) is never masked by the earlier "ok" line.
        engine_degraded: str | None = None
        for raw in raw_events:
            if str(raw.get("kind")) != "engine":
                continue
            data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
            value = data.get("degraded")
            if isinstance(value, str) and value.strip():
                engine_degraded = value.strip()

        # Test evidence → ForgeFlow owns the verdict (Q4).
        test_stdout = ""
        test_exit: int | None = None
        test_cases: list[dict[str, Any]] | None = None
        test_present = False
        for raw in raw_events:
            if str(raw.get("kind")) == "test":
                test_present = True
                data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
                if isinstance(data.get("raw_stdout"), str):
                    test_stdout = data["raw_stdout"]
                te = data.get("exit_code")
                if isinstance(te, (int, float)) and not isinstance(te, bool):
                    test_exit = int(te)
                if isinstance(data.get("cases"), list):
                    test_cases = data["cases"]
        tests = evaluate_test_output(
            test_stdout, test_exit, test_cases, command=job.test_command
        )

        # Diff (only when the runner really produced one).
        diff = ""
        for raw in raw_events:
            if str(raw.get("kind")) == "diff":
                data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
                candidate = data.get("diff")
                if isinstance(candidate, str) and candidate:
                    diff = candidate

        # Degradation decision (ordered by severity — parse > engine > crash).
        if unparsed > 0:
            degraded: str | None = "parse_failed"
        elif engine_degraded:
            # Pass the engine's reported value through verbatim (a registered
            # value or an unknown one) — the front-end gives an unknown value a
            # neutral note rather than inventing a cause.
            degraded = engine_degraded
        elif test_present and not tests.measured:
            degraded = "parse_failed"
        elif exit_code != 0 and result_line is None:
            degraded = "runner_crashed"
        else:
            degraded = None

        if degraded is None:
            status = "ok"
        elif degraded in _UNAVAILABLE_DEGRADES:
            status = "unavailable"
        else:
            status = "error"

        raw_meta: dict[str, Any] = {
            "exit_code": exit_code,
            "event_count": len(raw_events),
            "unparsed_lines": unparsed,
            "stderr": (stderr or "")[:4000],
        }
        if exit_code != 0 and result_line is None and (stderr or "").strip():
            raw_meta["reason"] = (stderr or "").strip()[:4000]
        # INC29 T02 (§6) — the progress summary, built from the run's own real
        # evidence (the unified diff + the reviewed test verdict). Unmeasured
        # fields stay ``None`` inside it — never a fabricated ``0``.
        summary = summarize_timeline(timeline, diff=diff, tests=tests.to_dict())
        return CodeRunResult(
            status=status,
            timeline=timeline,
            diff=diff,
            tests=tests,
            exit_code=exit_code,
            degraded=degraded,
            raw=raw_meta,
            summary=summary,
        )

    def _degraded(self, degraded: str, reason: str) -> CodeRunResult:
        unavailable = degraded in _UNAVAILABLE_DEGRADES
        event = CodeEvent(
            seq=0,
            ts=utcnow().isoformat(),
            phase="engine_ready",
            kind="engine",
            status="unavailable" if unavailable else "error",
            label="代码执行引擎不可用" if unavailable else "代码执行异常",
            detail=reason,
            data={"degraded": degraded, "reason": reason},
        )
        return CodeRunResult(
            status="unavailable" if unavailable else "error",
            timeline=[event],
            diff="",
            tests=TestResult(command="", verdict="unmeasured", measured=False),
            exit_code=-1,
            degraded=degraded,
            raw={"reason": reason},
        )

    async def _kill(self, proc: Any) -> None:
        if proc is None or getattr(proc, "returncode", None) is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        except Exception:  # noqa: BLE001 — cancellation must never raise
            return
        # ``Popen.wait`` blocks ⇒ run it in a worker thread. Escalation semantics
        # are unchanged: terminate → wait 5s → kill → wait 5s.
        try:
            await asyncio.to_thread(proc.wait, 5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001 — cancellation must never raise
                pass
            try:
                await asyncio.to_thread(proc.wait, 5)
            except subprocess.TimeoutExpired:
                pass


_ENGINE: Any = None


# --------------------------------------------------------------------------- #
# INC29 T04 (§4/§5) — the thin agent-server transport                           #
# --------------------------------------------------------------------------- #
#: SDK event class names that describe an **engine/transport-level** run failure —
#: the only kind that ends an entire run abnormally. ``AgentErrorEvent`` is
#: deliberately absent: it is a *single step* inside the loop (one failed edit, one
#: command timing out), already relayed honestly as a timeline step with
#: ``status="error"`` by ``events.py::adapt_openhands_event``. Promoting it to a
#: whole-run ``degraded`` would let an ordinary in-loop error masquerade as a
#: transport death — a false attribution the subprocess path never makes, so the
#: two transports would disagree (INC29 T04 review D1).
_TRANSPORT_ERROR_EVENT_TYPES: frozenset[str] = frozenset(
    {"ConversationErrorEvent", "ServerErrorEvent"}
)
#: The one structured ``code`` (the exception class name) meaning "round budget".
_MAX_ROUND_CODES: frozenset[str] = frozenset({"maxiterationsreached"})

#: The official page-size cap for ``GET /api/conversations/{id}/events/search``.
#: ``openhands/agent_server/event_router.py::search_conversation_events`` declares
#: ``limit`` with ``gt=0, le=100`` — a client that sends more than 100 gets a
#: ``422`` from the official server. Kept as a named constant so the value is
#: checked (and counterfactually injectable) rather than buried in the request.
_EVENTS_PAGE_LIMIT = 100


@dataclass
class _EventCursor:
    """The REST event-page resume state (INC30 T1).

    Two independent cursors, because the two servers expose different contracts:

    * ``page_id`` — the official opaque resume token. The official REST search
      (``event_router.py::search_conversation_events``) has **no** ``after_seq``;
      it pages by echoing back the ``next_page_id`` it returned.
    * ``after_seq`` — a **declared client extension**. The official REST API has no
      such parameter (it exists only on the ``/sockets/session/{id}`` socket —
      ``session_socket.py::session_socket``); the bundled thin server honours it to
      resume against frames that carry ``seq``. It is always sent so a single client
      works against both implementations — the official server simply ignores it.
    """

    after_seq: int = -1
    page_id: str | None = None


def _frame_key(frame: dict[str, Any]) -> str:
    """A stable identity for a raw frame — the official ``id`` or the thin ``seq``.

    The official ``Event`` (``openhands/sdk/event/base.py::Event``) has **no**
    ``seq`` field — only ``id`` + ``timestamp`` — while the bundled thin server
    numbers frames with a monotonic ``seq``. De-duplication must therefore key off
    whichever identity the server actually provided (``id`` wins when both are
    present), so neither implementation yields duplicates nor drops frames.
    """
    raw_id = frame.get("id")
    if raw_id is not None and str(raw_id):
        return f"id:{raw_id}"
    seq = frame.get("seq")
    if isinstance(seq, int) and not isinstance(seq, bool):
        return f"seq:{seq}"
    return f"obj:{id(frame)}"


class _AgentServerUnavailable(RuntimeError):
    """The agent server could not be reached, launched, or health-checked."""


def _free_port() -> int:
    """A currently-free loopback TCP port (bind 0, read it, release)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _degraded_from_frames(frames: list[dict[str, Any]]) -> str | None:
    """Derive a registered ``degraded`` value from the raw frames (§2.3).

    ``degraded`` is a **client-side** derivation from *transport facts* — the server
    never emits it, and it is reserved for a failure that ends the **whole run**.
    Only a conversation/server-level error event qualifies, and the decision is made
    on the structured ``code`` (the exception class name), **never** by scanning the
    free-text ``detail`` (INC29 T04 review D1: a plain in-loop command timeout — an
    ``AgentErrorEvent`` — must not be reported to the user as "the model is
    unavailable"; it is already shown as an ``error`` step). Values are restricted
    to :data:`forgeflow.codeplane.protocol.DEGRADED_VALUES`.
    """
    for frame in frames:
        if str(frame.get("type") or "") not in _TRANSPORT_ERROR_EVENT_TYPES:
            continue
        code = str(frame.get("code") or "").strip().lower()
        if code in _MAX_ROUND_CODES:
            return "max_rounds"
        return "runner_crashed"
    return None


class AgentServerOpenHandsEngine:
    """Drive OpenHands through the thin agent server over HTTP (INC29 §4/§5).

    REST polling (``httpx``) is the primary subscription path — zero-new-dependency
    and proxy-robust — with the server's WebSocket available as an optional
    enhancement. When ``codeplane_agent_server_url`` is set the engine talks to
    that already-running service; otherwise it launches the thin server on demand
    (loopback, per-process ``SESSION_API_KEY``) and reclaims it afterwards.

    This class lives in ``forgeflow/**`` (not ``runner/**``) so it is bound by the
    control-plane red line: it **must not** ``import openhands`` — it only speaks
    the wire contract, exactly like :class:`SubprocessOpenHandsEngine`.
    """

    def __init__(
        self,
        settings: Any = None,
        *,
        url: str | None = None,
        token: str | None = None,
        interpreter: str | None = None,
        server_argv: list[str] | None = None,
        port: int | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        configured_url = url if url is not None else getattr(self._settings, "codeplane_agent_server_url", "") or ""
        self._external_url = str(configured_url).rstrip("/")
        configured_token = token if token is not None else getattr(self._settings, "codeplane_agent_server_token", "") or ""
        self._token = str(configured_token).strip()
        explicit = interpreter if interpreter is not None else ""
        self._interpreter = (
            explicit
            or getattr(self._settings, "codeplane_interpreter", "")
            or os.environ.get("FORGEFLOW_CODEPLANE_PYTHON", "")
        ).strip()
        argv = server_argv if server_argv else [str(_DEFAULT_SERVER_ENTRY)]
        self._server_argv = [str(a) for a in argv]
        self._explicit_port = port
        self._allow_external = bool(getattr(self._settings, "codeplane_agent_server_allow_external", False))
        self._startup_s = int(getattr(self._settings, "codeplane_agent_server_startup_s", 15) or 15)
        self._enabled = bool(getattr(self._settings, "codeplane_enabled", True))
        self._reason = ""
        self._proc: Any = None
        self._owned = False

    # ---------------------------------------------------------------- #
    # Availability                                                     #
    # ---------------------------------------------------------------- #
    def available(self) -> bool:
        """True when the configured agent server is usable (cheap probe)."""
        if not self._enabled:
            self._reason = "代码执行面已关闭（Settings.codeplane_enabled=false）"
            return False
        if self._external_url:
            ok, reason = self._health_get(self._external_url, self._token)
            if not ok:
                self._reason = reason
            return ok
        if not self._interpreter:
            self._reason = (
                "未配置代码执行面解释器"
                "（Settings.codeplane_interpreter / FORGEFLOW_CODEPLANE_PYTHON 均为空），"
                "无法按需拉起 agent server"
            )
            return False
        if not _interpreter_ok(self._interpreter):
            self._reason = f"代码执行面解释器不存在：{self._interpreter}"
            return False
        return True

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def interpreter(self) -> str:
        return self._interpreter

    # ---------------------------------------------------------------- #
    # HTTP helpers                                                     #
    # ---------------------------------------------------------------- #
    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if token:
            headers["X-Session-API-Key"] = token
        return headers

    @staticmethod
    def _env(token: str) -> dict[str, str]:
        """Child environment with proxies stripped (reuses the §9 hygiene)."""
        env = dict(os.environ)
        for name in _PROXY_VARS:
            env.pop(name, None)
        env["NO_PROXY"] = _NO_PROXY
        env["no_proxy"] = _NO_PROXY
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env[_SESSION_API_KEY_ENV] = token
        return env

    def _health_get(self, base: str, token: str, timeout: float = 3.0) -> tuple[bool, str]:
        import httpx

        try:
            resp = httpx.get(f"{base}{_HEALTH_PATH}", headers=self._headers(token), timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — unreachable is a result, not a crash
            return False, f"agent server 不可达：{exc!r}"
        if resp.status_code == 200:
            return True, ""
        return False, f"agent server 健康检查 HTTP {resp.status_code}"

    # ---------------------------------------------------------------- #
    # Run                                                              #
    # ---------------------------------------------------------------- #
    async def run(self, job: CodeJob) -> CodeRunResult:
        if not self._enabled:
            return self._degraded(
                "engine_unavailable", "代码执行面已关闭（Settings.codeplane_enabled=false）", unstarted=True
            )
        job = _merge_job_defaults(job, self._settings)
        try:
            base, token = await self._ensure_server()
        except _AgentServerUnavailable as exc:
            return self._degraded("engine_unavailable", str(exc), unstarted=True)
        try:
            return await self._drive(base, token, job)
        finally:
            self._reclaim()

    async def cancel(self, handle: Any = None) -> None:
        await asyncio.to_thread(self._kill_proc_sync)

    # ---------------------------------------------------------------- #
    # Server lifecycle                                                 #
    # ---------------------------------------------------------------- #
    async def _ensure_server(self) -> tuple[str, str]:
        """Return ``(base_url, token)`` for an external or freshly-launched server."""
        if self._external_url:
            self._owned = False
            ok, reason = await asyncio.to_thread(self._health_get, self._external_url, self._token)
            if not ok:
                raise _AgentServerUnavailable(reason)
            return self._external_url, self._token

        port = self._explicit_port or _free_port()
        token = self._token or secrets.token_urlsafe(32)
        argv = [self._interpreter, *self._server_argv, "--host", "127.0.0.1", "--port", str(port)]
        env = self._env(token)
        try:
            self._proc = subprocess.Popen(
                argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env
            )
        except (OSError, ValueError) as exc:
            raise _AgentServerUnavailable(f"无法启动 agent server：{exc!r}") from exc
        self._owned = True

        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + max(1, self._startup_s)
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                tail = self._read_tail()
                raise _AgentServerUnavailable(
                    f"agent server 启动失败（进程已退出，code={self._proc.returncode}）：{tail}"
                )
            ok, _reason = await asyncio.to_thread(self._health_get, base, token, 1.0)
            if ok:
                return base, token
            await asyncio.sleep(0.2)
        tail = self._read_tail()
        raise _AgentServerUnavailable(
            f"agent server 在 {max(1, self._startup_s)}s 内未就绪：{tail}"
        )

    def _read_tail(self, limit: int = 4000) -> str:
        """Stop the child (if alive) and return its captured stderr tail."""
        proc = self._proc
        if proc is None:
            return ""
        try:
            if proc.poll() is None:
                proc.terminate()
            try:
                _out, err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                _out, err = proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001 — best-effort diagnostics
            return ""
        return (err or b"").decode("utf-8", "replace")[-limit:]

    def _kill_proc_sync(self) -> None:
        proc = self._proc
        if proc is None or getattr(proc, "returncode", None) is not None:
            return
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001 — cancellation must never raise
            return
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                pass

    def _reclaim(self) -> None:
        if self._owned and self._proc is not None:
            self._kill_proc_sync()
        self._proc = None
        self._owned = False

    # ---------------------------------------------------------------- #
    # Drive one task                                                   #
    # ---------------------------------------------------------------- #
    def _start_payload(self, job: CodeJob) -> dict[str, Any]:
        """The ``StartConversationRequest`` body (official field names).

        Carries the platform-selected context (``skill_context`` / ``memory_context``)
        into the ``agent`` spec as the **raw lists** — the client does not render
        them; the server appends the rendered block to the system prompt via the one
        shared renderer (``runner/context_block.py``). Without this the model would
        never see the skills/memory ForgeFlow chose, while the platform still
        reported them as injected (INC29 T04 review D2).

        Discriminators (INC30 T1) — the official request types both the workspace and
        the agent as discriminated-union members. ``workspace`` is a concrete
        ``openhands/sdk/workspace/local.py::LocalWorkspace`` and ``agent`` is the
        abstract ``openhands/sdk/agent/base.py::AgentBase``, both deriving from
        ``openhands/sdk/utils/models.py::DiscriminatedUnionMixin`` whose
        ``@computed_field kind`` is the **class name**. The official server rejects a
        bare ``{"working_dir": ...}`` workspace with a ``422`` and needs the agent's
        ``kind`` to route the union, so both discriminator fields are sent verbatim:

          * ``workspace.kind`` = ``"LocalWorkspace"``
            (``openhands/sdk/workspace/local.py::LocalWorkspace``);
          * ``agent.kind`` = ``"Agent"``
            (``openhands/sdk/agent/agent.py::Agent`` — the concrete kind the
            ``AgentBase`` field of
            ``openhands/sdk/conversation/request.py::StartConversationRequest``
            resolves to).

        The ``llm`` / ``tools`` / ``skill_context`` / ``memory_context`` payload is
        unchanged: those fields are consumed by the thin service
        (``runner/agent_server/conversation.py`` + ``runner/context_block.py``).
        """
        return {
            "agent": {
                "kind": "Agent",
                "llm": {
                    "model": job.model,
                    "base_url": job.base_url,
                    "api_key": job.api_key,
                    "reasoning_effort": job.reasoning_effort or "none",
                    "temperature": job.temperature if job.temperature is not None else 0.0,
                    "litellm_extra_body": {"num_ctx": int(job.num_ctx or 32768)},
                },
                "tools": ["terminal", "file_editor"],
                "skill_context": list(job.skill_context),
                "memory_context": list(job.memory_context),
            },
            "workspace": {
                "kind": "LocalWorkspace",
                "working_dir": job.workspace_path,
            },
            "initial_message": {
                "role": "user",
                "content": [{"type": "text", "text": job.task_intent}],
                "run": False,
            },
            "max_iterations": int(job.max_rounds or 0),
        }

    async def _drive(self, base: str, token: str, job: CodeJob) -> CodeRunResult:
        import httpx

        wall = int(job.wall_timeout_s or getattr(self._settings, "codeplane_timeout_seconds", 180))
        deadline = time.monotonic() + wall
        frames: list[dict[str, Any]] = []
        cursor = _EventCursor()
        exec_status = ""
        conversation_id = ""
        try:
            async with httpx.AsyncClient(
                base_url=base, headers=self._headers(token), timeout=httpx.Timeout(30.0, read=60.0)
            ) as client:
                # C1 — create the conversation.
                resp = await client.post("/api/conversations", json=self._start_payload(job))
                if resp.status_code not in (200, 201):
                    return self._degraded(
                        "engine_unavailable",
                        f"建立会话失败：HTTP {resp.status_code} {resp.text[:300]}",
                        unstarted=True,
                    )
                try:
                    conversation_id = str((resp.json() or {}).get("id") or "")
                except Exception:  # noqa: BLE001
                    conversation_id = ""
                if not conversation_id:
                    return self._degraded("engine_unavailable", "建立会话响应缺少 id", unstarted=True)

                # C4 — run it.
                run_resp = await client.post(f"/api/conversations/{conversation_id}/run")
                if run_resp.status_code not in (200, 201):
                    return self._degraded(
                        "engine_unavailable",
                        f"启动会话失败：HTTP {run_resp.status_code} {run_resp.text[:300]}",
                        unstarted=True,
                    )

                # REST polling (the primary subscription path).
                timed_out = False
                while True:
                    cursor, frames = await self._poll_events(client, conversation_id, cursor, frames)
                    exec_status = await self._poll_status(client, conversation_id, exec_status)
                    if exec_status in ("finished", "error"):
                        cursor, frames = await self._poll_events(client, conversation_id, cursor, frames)
                        break
                    if time.monotonic() > deadline:
                        timed_out = True
                        break
                    await asyncio.sleep(0.25)

                # Evidence — gathered while still connected to the server.
                tests, test_exit = await self._run_tests(client, job)
                diff = self._local_git_diff(job.workspace_path)
                try:
                    await client.delete(f"/api/conversations/{conversation_id}")
                except Exception:  # noqa: BLE001 — reclaim is best-effort
                    pass

            if timed_out:
                return self._build_result(
                    frames, job, conversation_id, exec_status, tests, test_exit, diff,
                    "timeout", f"agent server 在 {wall}s 内未达终态",
                )
            return self._build_result(
                frames, job, conversation_id, exec_status, tests, test_exit, diff,
                _degraded_from_frames(frames), "",
            )
        except Exception as exc:  # noqa: BLE001 — the transport died: never fake success
            diff = self._local_git_diff(job.workspace_path)
            tests = TestResult(command=job.test_command, verdict="unmeasured", measured=False)
            return self._build_result(
                frames, job, conversation_id, exec_status, tests, None, diff,
                "runner_crashed", repr(exc),
            )

    async def _poll_events(
        self,
        client: Any,
        conversation_id: str,
        cursor: _EventCursor,
        frames: list[dict[str, Any]],
    ) -> tuple[_EventCursor, list[dict[str, Any]]]:
        """Fetch the next page of event frames and append the new ones (REST polling).

        The request is kept **official-legal** (INC30 T1): ``page_id`` (the opaque
        resume cursor taken from the previous ``next_page_id``) plus ``limit`` bounded
        by the official cap (``event_router.py::search_conversation_events`` declares
        ``gt=0, le=100``). ``after_seq`` rides along as an **explicitly declared
        extension** — the official REST API has no such parameter — so the very same
        code also resumes against the bundled thin server, whose frames carry ``seq``.

        Frame identity is taken from the official ``id`` when present, else the thin
        server's ``seq`` (:func:`_frame_key`), so de-duplication and the ``after_seq``
        cursor work against **both** implementations without ever assuming ``seq``
        exists. The display sequence number remains :meth:`_build_result`'s
        ``enumerate`` — this method only advances the transport cursor.

        A transport failure is **not** swallowed: it propagates to :meth:`_drive`,
        which records ``runner_crashed`` with the partial timeline it already has
        — never a fabricated success (A5).
        """
        params: dict[str, Any] = {"limit": _EVENTS_PAGE_LIMIT}
        if cursor.page_id:
            params["page_id"] = cursor.page_id
        params["after_seq"] = cursor.after_seq  # declared extension; official: N/A
        resp = await client.get(
            f"/api/conversations/{conversation_id}/events/search", params=params
        )
        if resp.status_code != 200:
            return cursor, frames
        body = resp.json() or {}
        seen = {_frame_key(f) for f in frames}
        after_seq = cursor.after_seq
        for item in body.get("items") or []:
            if not isinstance(item, dict):
                continue
            key = _frame_key(item)
            if key in seen:
                continue
            seen.add(key)
            frames.append(item)
            seq = item.get("seq")
            if isinstance(seq, int) and not isinstance(seq, bool):
                after_seq = max(after_seq, seq)
        next_page_id = body.get("next_page_id") or None
        return _EventCursor(after_seq=after_seq, page_id=next_page_id), frames

    async def _poll_status(self, client: Any, conversation_id: str, current: str) -> str:
        """Read the authoritative ``execution_status`` (the run's terminal signal)."""
        resp = await client.get(f"/api/conversations/{conversation_id}")
        if resp.status_code == 200:
            return str((resp.json() or {}).get("execution_status") or current)
        return current

    async def _run_tests(self, client: Any, job: CodeJob) -> tuple[TestResult, int | None]:
        """Run the test command via ``C18`` and review the RAW output (ForgeFlow owns the verdict)."""
        command = (job.test_command or "").strip()
        unmeasured = TestResult(command=command, verdict="unmeasured", measured=False)
        if not command:
            return unmeasured, None
        try:
            resp = await client.post(
                "/api/bash/execute_bash_command",
                json={
                    "command": command,
                    "cwd": job.workspace_path or None,
                    "timeout": max(30, int(job.wall_timeout_s or 180)),
                },
            )
        except Exception:  # noqa: BLE001
            return unmeasured, None
        if resp.status_code != 200:
            return unmeasured, None
        data = resp.json() or {}
        stdout = str(data.get("stdout") or "")
        stderr = str(data.get("stderr") or "")
        if stderr:
            stdout = f"{stdout}\n{stderr}"
        raw_exit = data.get("exit_code")
        exit_code = (
            int(raw_exit) if isinstance(raw_exit, int) and not isinstance(raw_exit, bool) else None
        )
        return evaluate_test_output(stdout, exit_code, None, command=command), exit_code

    @staticmethod
    def _local_git_diff(workspace: str) -> str:
        """The workspace's staged unified diff (mirrors the runner's evidence step)."""
        if not workspace:
            return ""
        try:
            subprocess.run(
                ["git", "-C", workspace, "add", "-A"],
                capture_output=True, timeout=30, check=False,
            )
            completed = subprocess.run(
                ["git", "-C", workspace, "diff", "--cached", "--no-color"],
                capture_output=True, text=True, timeout=30, check=False,
            )
            return completed.stdout or ""
        except Exception:  # noqa: BLE001 — a missing git is not a crash
            return ""

    def _build_result(
        self,
        frames: list[dict[str, Any]],
        job: CodeJob,
        conversation_id: str,
        exec_status: str,
        tests: TestResult,
        test_exit: int | None,
        diff: str,
        degraded: str | None,
        reason: str,
    ) -> CodeRunResult:
        timeline = [adapt_openhands_event(frame, seq=i) for i, frame in enumerate(frames)]
        if degraded is None:
            status = "ok"
        elif degraded in _UNAVAILABLE_DEGRADES:
            status = "unavailable"
        else:
            status = "error"
        summary = summarize_timeline(timeline, diff=diff, tests=tests.to_dict())
        raw: dict[str, Any] = {
            "event_count": len(frames),
            "conversation_id": conversation_id,
            "execution_status": exec_status,
        }
        if reason:
            raw["reason"] = reason
        return CodeRunResult(
            status=status,
            timeline=timeline,
            diff=diff,
            tests=tests,
            exit_code=test_exit if test_exit is not None else -1,
            degraded=degraded,
            raw=raw,
            summary=summary,
            transport="agent_server",
        )

    def _degraded(self, degraded: str, reason: str, *, unstarted: bool = False) -> CodeRunResult:
        unavailable = degraded in _UNAVAILABLE_DEGRADES
        event = CodeEvent(
            seq=0,
            ts=utcnow().isoformat(),
            phase="engine_ready",
            kind="engine",
            status="unavailable" if unavailable else "error",
            label="代码执行引擎不可用" if unavailable else "代码执行异常",
            detail=reason,
            data={"degraded": degraded, "reason": reason},
        )
        raw: dict[str, Any] = {"reason": reason}
        if unstarted:
            raw["agent_server_unstarted"] = True
        return CodeRunResult(
            status="unavailable" if unavailable else "error",
            timeline=[event],
            diff="",
            tests=TestResult(command="", verdict="unmeasured", measured=False),
            exit_code=-1,
            degraded=degraded,
            raw=raw,
            transport="agent_server",
        )


class _AutoFallbackEngine:
    """``transport="auto"`` — prefer the agent server, honestly fall back (§5.3).

    When the agent server is unavailable (or could not even be started), the task
    runs on the subprocess engine and the result records
    ``transport="subprocess"`` + ``fell_back_from="agent_server"`` — never a silent
    cross-stack switch. A run that *did* start on the agent server is never
    re-run: its result (success or degrade) is returned as-is.
    """

    def __init__(self, primary: Any, fallback: Any) -> None:
        self._primary = primary
        self._fallback = fallback
        self._reason = ""

    def available(self) -> bool:
        return self._primary.available() or self._fallback.available()

    @property
    def reason(self) -> str:
        return getattr(self._primary, "reason", "") or getattr(self._fallback, "reason", "")

    @property
    def interpreter(self) -> str:
        return getattr(self._fallback, "interpreter", "")

    async def run(self, job: CodeJob) -> CodeRunResult:
        if self._primary.available():
            result = await self._primary.run(job)
            if not result.raw.get("agent_server_unstarted"):
                return result
        result = await self._fallback.run(job)
        result.fell_back_from = "agent_server"
        return result

    async def cancel(self, handle: Any = None) -> None:
        await self._primary.cancel(handle)
        await self._fallback.cancel(handle)


def _build_engine(settings: Any) -> Any:
    """Select the engine for ``Settings.codeplane_transport`` (§5.2)."""
    transport = str(getattr(settings, "codeplane_transport", "subprocess") or "subprocess").strip().lower()
    if transport == "agent_server":
        return AgentServerOpenHandsEngine(settings=settings)
    if transport == "auto":
        return _AutoFallbackEngine(
            AgentServerOpenHandsEngine(settings=settings),
            SubprocessOpenHandsEngine(settings=settings),
        )
    return SubprocessOpenHandsEngine(settings=settings)


def get_code_engine() -> Any:
    """Process-wide engine selected by ``Settings.codeplane_transport``.

    ``"subprocess"`` (default) ⇒ :class:`SubprocessOpenHandsEngine` — today's
    byte-identical behaviour. ``"agent_server"`` ⇒ the thin agent server.
    ``"auto"`` ⇒ try the agent server, else fall back to the subprocess with
    ``fell_back_from`` recorded. Test helper: :func:`reset_code_engine`.
    """
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = _build_engine(get_settings())
    return _ENGINE


def reset_code_engine() -> None:
    """Drop the process-wide engine. Test helper."""
    global _ENGINE
    _ENGINE = None
